"""Real-socket backend harness shared by the streaming E2E and per-preset contract suites.

``preset_wire`` is the ONE place that knows how each backend preset frames a reply on
the wire. ``ScriptedBackend`` is a real aiohttp server on 127.0.0.1 (started by the
``live_backend`` fixture in ``conftest.py``) that replays a scripted reply with real
delays between frames, so adapters run against genuine sockets, TCP chunking, and
aiohttp client timeouts — not a mocked session. ``setup_agent`` / ``run_pipeline_turn``
drive a turn exactly as ``assist_pipeline`` does: a chat log with a delta listener
around ``conversation.async_converse``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import chat_session
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_middleman.backends import BACKEND_TO_CLS
from custom_components.llm_middleman.const import CONF_NAME, DOMAIN

# Every registered preset; a new adapter is picked up by both suites automatically.
PRESETS = sorted(BACKEND_TO_CLS)
# Presets framed as SSE (bare-CR line endings are legal there, not in NDJSON).
SSE_PRESETS = frozenset({"converse", "dify", "langgraph", "openai_compat"})
_NDJSON_CONTENT_TYPE = {"ollama": "application/x-ndjson", "n8n": "application/json-lines"}

# Every path an adapter streams a turn from (all served by ``ScriptedBackend._turn``).
_TURN_PATHS = (
    "/v1/chat/completions",  # openai_compat
    "/api/chat",  # ollama
    "/threads/{thread_id}/runs/stream",  # langgraph (threaded)
    "/runs/stream",  # langgraph (stateless)
    "/chat-messages",  # dify
    "/v1/converse",  # converse
    "/webhook",  # n8n
)


def content_type(preset: str) -> str:
    """Return the response ``Content-Type`` a real ``preset`` backend streams with."""
    return _NDJSON_CONTENT_TYPE.get(preset, "text/event-stream")


@dataclass
class Wire:
    """One reply in a preset's wire format: setup bytes, one frame per piece, terminator."""

    prologue: list[bytes]
    frames: list[bytes]
    epilogue: list[bytes]

    def blob(self) -> bytes:
        """Return the whole reply as one byte string."""
        return b"".join([*self.prologue, *self.frames, *self.epilogue])


def preset_wire(preset: str, pieces: list[str], newline: bytes = b"\n") -> Wire:
    """Frame ``pieces`` (the reply text, one frame each) as ``preset``'s backend sends them."""

    def sse(data: object, event: str | None = None) -> bytes:
        head = b"event: " + event.encode() + newline if event is not None else b""
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return head + b"data: " + payload.encode() + newline + newline

    def line(obj: object) -> bytes:
        return json.dumps(obj, ensure_ascii=False).encode() + newline

    match preset:
        case "openai_compat":
            return Wire(
                [sse({"choices": [{"delta": {"role": "assistant"}}]})],
                [sse({"choices": [{"delta": {"content": p}}]}) for p in pieces],
                [sse("[DONE]")],
            )
        case "ollama":
            return Wire(
                [],
                [line({"message": {"role": "assistant", "content": p}, "done": False}) for p in pieces],
                [line({"message": {"role": "assistant", "content": ""}, "done": True})],
            )
        case "langgraph":  # stream_mode=messages-tuple; success ends at EOF
            return Wire(
                [sse({"run_id": "r1"}, "metadata")],
                [
                    sse([{"content": p, "type": "AIMessageChunk"}, {"langgraph_node": "agent"}], "messages")
                    for p in pieces
                ],
                [],
            )
        case "dify":
            # Documented Agent-app wire: bare `data:` JSON with the event name INSIDE (no
            # `event:` line, so the SSE field reads "message"), `agent_message` deltas, then
            # one closing `message` repeating the full answer that must not be re-spoken.
            ids = {"task_id": "task1", "conversation_id": "c1", "message_id": "m1"}
            return Wire(
                [b"event: ping" + newline + newline],
                [sse({"event": "agent_message", "answer": p, **ids}) for p in pieces],
                [sse({"event": "message", "answer": "".join(pieces), **ids}), sse({"event": "message_end", **ids})],
            )
        case "n8n":  # streaming StructuredChunk NDJSON; EOF is the done signal
            return Wire(
                [line({"type": "begin", "metadata": {}})],
                [line({"type": "item", "content": p, "metadata": {}}) for p in pieces],
                [line({"type": "end", "metadata": {}})],
            )
        case "converse":
            return Wire(
                [],
                [sse({"delta": p}, "text_delta") for p in pieces],
                [sse({"text": "".join(pieces), "continue_conversation": False}, "done")],
            )
        case _:
            raise ValueError(f"No wire format for preset {preset!r} — add it to preset_wire")


def entry_data(preset: str, base_url: str) -> dict[str, Any]:
    """Return parent-entry connection data pointing ``preset`` at ``base_url``."""
    if preset == "n8n":
        return {"backend_type": preset, "webhook_url": f"{base_url}/webhook", "target_type": "chat_trigger"}
    if preset == "converse":
        return {"backend_type": preset, "base_url": base_url, "token": "test-token"}
    return {"backend_type": preset, "base_url": base_url, "api_key": "test-key"}


@dataclass
class _Script:
    wire: Wire
    delays: list[float]  # seconds slept before each frame
    content_type: str
    status: int


@dataclass
class ScriptedBackend:
    """A live aiohttp server replaying one scripted reply per turn request.

    Records the monotonic time of every frame write, and sets ``disconnected`` when the
    client drops the connection mid-reply (a cancelled turn must release it).
    """

    base_url: str = ""
    writes: list[float] = field(default_factory=list[float])
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    _script: _Script | None = None

    def serve(
        self,
        preset: str,
        pieces: list[str],
        *,
        delays: list[float] | None = None,
        status: int = 200,
    ) -> None:
        """Script the next turn(s): ``preset``-framed ``pieces``, or an HTTP ``status`` error."""
        self.writes.clear()
        self.disconnected.clear()
        self._script = _Script(
            wire=preset_wire(preset, pieces),
            delays=delays if delays is not None else [0.0] * len(pieces),
            content_type=content_type(preset),
            status=status,
        )

    def app(self) -> web.Application:
        """Build the aiohttp app serving every preset's endpoints."""
        app = web.Application()
        app.router.add_post("/threads", self._thread)
        app.router.add_post("/chat-messages/{task_id}/stop", self._ok)
        for path in _TURN_PATHS:
            app.router.add_post(path, self._turn)
        # Connection probes async_setup_entry runs (converse accepts any HTTP response).
        app.router.add_get("/v1/models", self._probe({"data": []}))
        app.router.add_get("/api/tags", self._probe({"models": []}))
        app.router.add_post("/assistants/search", self._probe([{"assistant_id": "a1", "graph_id": "agent"}]))
        app.router.add_get("/info", self._probe({"mode": "advanced-chat"}))
        return app

    @staticmethod
    def _probe(body: object) -> Callable[[web.Request], Awaitable[web.Response]]:
        async def handler(request: web.Request) -> web.Response:
            return web.json_response(body)

        return handler

    async def _thread(self, request: web.Request) -> web.Response:
        return web.json_response({"thread_id": "t1"})

    async def _ok(self, request: web.Request) -> web.Response:
        return web.json_response({"result": "success"})

    async def _client_left_during(self, request: web.Request, seconds: float) -> bool:
        """Sleep ``seconds``, polling the transport; True (and flag it) if the client left."""
        deadline = time.monotonic() + seconds
        while True:
            transport = request.transport
            if transport is None or transport.is_closing():
                self.disconnected.set()
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.005)

    async def _turn(self, request: web.Request) -> web.StreamResponse:
        script = self._script
        if script is None:
            raise RuntimeError("ScriptedBackend.serve() was not called before the turn")
        if script.status != 200:
            return web.json_response({"error": {"message": "scripted failure"}}, status=script.status)
        response = web.StreamResponse(headers={"Content-Type": script.content_type})
        await response.prepare(request)
        try:
            for chunk in script.wire.prologue:
                await response.write(chunk)
            for frame, delay in zip(script.wire.frames, script.delays, strict=True):
                if await self._client_left_during(request, delay):
                    return response
                await response.write(frame)
                self.writes.append(time.monotonic())
            for chunk in script.wire.epilogue:
                await response.write(chunk)
            await response.write_eof()
        except ConnectionResetError:
            self.disconnected.set()
        return response


async def setup_agent(hass: HomeAssistant, preset: str, base_url: str, **options: Any) -> tuple[MockConfigEntry, str]:
    """Set up a ``preset`` entry with one conversation agent; return it and the entity id."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=entry_data(preset, base_url),
        subentries_data=[
            ConfigSubentryData(
                data={CONF_NAME: "Agent", **options}, subentry_type="conversation", title="Agent", unique_id=None
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("conversation", DOMAIN, next(iter(entry.subentries)))
    assert entity_id is not None
    return entry, entity_id


@dataclass
class DeltaRecorder:
    """Collects ``(monotonic time, delta)`` for every chat-log delta of a turn."""

    events: list[tuple[float, Mapping[str, Any]]] = field(default_factory=list[tuple[float, Mapping[str, Any]]])
    first_content: asyncio.Event = field(default_factory=asyncio.Event)

    def __call__(self, _chat_log: conversation.ChatLog, delta: dict[str, Any]) -> None:
        self.events.append((time.monotonic(), delta))
        if delta.get("content"):
            self.first_content.set()

    @property
    def content_times(self) -> list[float]:
        """Arrival times of the deltas that carried non-empty content."""
        return [at for at, delta in self.events if delta.get("content")]

    @property
    def contents(self) -> list[str]:
        """The non-empty content strings, in arrival order."""
        return [str(delta["content"]) for _, delta in self.events if delta.get("content")]


async def run_pipeline_turn(
    hass: HomeAssistant,
    entity_id: str,
    recorder: Callable[[conversation.ChatLog, dict[str, Any]], None] | None = None,
    text: str = "hi",
) -> conversation.ConversationResult:
    """Run one turn the way ``assist_pipeline`` does: delta-listening chat log + async_converse."""
    with chat_session.async_get_chat_session(hass, None) as session:
        user_input = conversation.ConversationInput(
            text=text,
            context=Context(),
            conversation_id=session.conversation_id,
            device_id=None,
            satellite_id=None,
            language="en",
            agent_id=entity_id,
        )
        # chat_log_delta_listener's dict param is untyped upstream in HA.
        with conversation.async_get_chat_log(  # pyright: ignore[reportUnknownMemberType]
            hass, session, user_input, chat_log_delta_listener=recorder
        ):
            return await conversation.async_converse(
                hass, text, session.conversation_id, Context(), language="en", agent_id=entity_id
            )


def speech(result: conversation.ConversationResult) -> str:
    """Return the plain-text speech of a conversation result."""
    return str(result.response.speech["plain"]["speech"])
