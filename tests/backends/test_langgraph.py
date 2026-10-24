"""Tests for the LangGraph adapter (LLMM-011, tier-1 rework LLMM-022).

Streams are **real wire captures** from a ``langgraph-api`` 0.16.6 server
(``tests/backends/fixtures/langgraph/*.json``, raw body bytes kept verbatim incl. CRLF),
replayed through the real ``_sse`` parser + adapter at arbitrary chunk boundaries. A
small fake session (``_FakeServer``) routes the adapter's requests the way the server
does: run POSTs stream the next capture, ``GET /threads/{id}`` returns thread JSON, and
cancel POSTs answer 202.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.const import CONF_PROMPT
from homeassistant.core import Context, HomeAssistant

from custom_components.llm_middleman.backends.base import (
    BackendAuthError,
    BackendConnectionError,
    BackendStreamError,
    DeltaStream,
    TurnContext,
)
from custom_components.llm_middleman.backends.langgraph import (
    AssistantNotFoundError,
    LangGraphAdapter,
    _extract_text,
    _parse_messages_frame,
    _parse_updates_frame,
)
from custom_components.llm_middleman.const import ERROR_MESSAGE
from tests.conftest import MockChatLog, _FakeStreamContent, build_entity, chunk_bytes, sse_bytes

_BASE_URL = "http://lg.local:2024"
_CONN: dict[str, Any] = {"base_url": _BASE_URL, "api_key": None, "assistant_id": "agent", "entry_id": "e1"}
_FIXTURES = Path(__file__).parent / "fixtures" / "langgraph"
_ANSWER = "It is sunny and 21C in Paris."  # the react capture's spoken reply
_QUESTION = "Which room should I turn the lights on in?"  # the interrupt capture's question


def _capture(name: str) -> bytes:
    """Return the exact response body bytes of a recorded run."""
    return cast("str", json.loads((_FIXTURES / f"{name}.json").read_text())["body"]).encode()


def _thread_json(name: str = "thread_interrupted") -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads((_FIXTURES / f"{name}.json").read_text()))


def _events_until(blob: bytes, marker: bytes) -> bytes:
    """Truncate a capture after the first SSE event containing ``marker``."""
    events = blob.split(b"\r\n\r\n")
    index = next(i for i, event in enumerate(events) if marker in event)
    return b"\r\n\r\n".join(events[: index + 1]) + b"\r\n\r\n"


# --- fake aiohttp responses / server -----------------------------------------


class _StreamResp:
    """Streaming response CM: ``status`` + ``content.iter_any()`` + ``text()``."""

    def __init__(self, content: Any, *, status: int = 200) -> None:
        self.status = status
        self.content = content

    async def text(self) -> str:
        return ""

    async def __aenter__(self) -> _StreamResp:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _JsonResp:
    """JSON response CM: ``status`` + ``json()``."""

    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def __aenter__(self) -> _JsonResp:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _HangingContent:
    """Stream content that yields ``first`` then blocks until cancelled (a run in flight)."""

    def __init__(self, first: bytes) -> None:
        self._first = first
        self.started = asyncio.Event()

    async def iter_any(self) -> AsyncIterator[bytes]:
        yield self._first
        self.started.set()
        await asyncio.Event().wait()


class _FakeServer:
    """Session stand-in that routes adapter requests like a LangGraph Agent Server."""

    def __init__(self, *runs: bytes | _HangingContent, thread: object = None, chunk: int = 7) -> None:
        self._runs = list(runs)
        self._chunk = chunk
        self.thread = thread  # GET /threads/{id} body; None -> 404
        self.cancel_exc: Exception | None = None
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.session = MagicMock()
        self.session.post = MagicMock(side_effect=self._post)
        self.session.get = MagicMock(side_effect=self._get)

    def _post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/cancel"):
            if self.cancel_exc is not None:
                raise self.cancel_exc
            return _JsonResp(None, status=202)
        run = self._runs.pop(0)
        if isinstance(run, _HangingContent):
            return _StreamResp(run)
        return _StreamResp(_FakeStreamContent(chunk_bytes(run, self._chunk)))

    def _get(self, url: str, **kwargs: Any) -> _JsonResp:
        self.calls.append(("GET", url, kwargs))
        return _JsonResp(self.thread, status=404 if self.thread is None else 200)

    @property
    def run_calls(self) -> list[tuple[str, dict[str, Any]]]:
        return [(url, kw) for method, url, kw in self.calls if method == "POST" and url.endswith("/runs/stream")]

    @property
    def run_urls(self) -> list[str]:
        return [url for url, _ in self.run_calls]

    def body(self, index: int = -1) -> dict[str, Any]:
        return cast("dict[str, Any]", json.loads(self.run_calls[index][1]["data"]))

    @property
    def cancel_urls(self) -> list[str]:
        return [url for method, url, _ in self.calls if method == "POST" and url.endswith("/cancel")]


# --- turn helpers --------------------------------------------------------------


def _input(text: str = "hi", **kwargs: Any) -> conversation.ConversationInput:
    values: dict[str, Any] = {
        "text": text,
        "context": Context(),
        "conversation_id": "conv-1",
        "device_id": None,
        "satellite_id": None,
        "language": "en",
        "agent_id": "agent-1",
        **kwargs,
    }
    return conversation.ConversationInput(**values)


def _chat_log(system_prompt: str = "") -> MagicMock:
    # The adapter reads the rendered prompt (content[0]), conversation_id, and async_trace.
    chat_log = MagicMock()
    chat_log.content = [conversation.SystemContent(content=system_prompt)]
    chat_log.conversation_id = "conv-1"
    return chat_log


def _adapter(hass: HomeAssistant, server: _FakeServer, **conn: Any) -> LangGraphAdapter:
    return LangGraphAdapter(hass, cast("aiohttp.ClientSession", server.session), {**_CONN, **conn})


async def _turn(
    adapter: LangGraphAdapter,
    *,
    options: dict[str, Any] | None = None,
    key: str = "conv-1",
    legacy_key: str | None = None,
    text: str = "hi",
    chat_log: MagicMock | None = None,
    **input_kwargs: Any,
) -> tuple[list[conversation.AssistantContentDeltaDict], TurnContext]:
    ctx = TurnContext(options=options or {}, memory_key=key, legacy_memory_key=legacy_key)
    log = cast("conversation.ChatLog", chat_log if chat_log is not None else _chat_log())
    deltas = [delta async for delta in adapter.stream_turn(log, _input(text, **input_kwargs), ctx)]
    return deltas, ctx


async def _collect(gen: DeltaStream) -> list[conversation.AssistantContentDeltaDict]:
    return [delta async for delta in gen]


def _text_of(deltas: list[conversation.AssistantContentDeltaDict]) -> str:
    return "".join(delta.get("content") or "" for delta in deltas)


def _tool_calls(deltas: list[conversation.AssistantContentDeltaDict]) -> list[Any]:
    return [call for delta in deltas for call in delta.get("tool_calls") or []]


def _patch_client_session(mp: pytest.MonkeyPatch, session: MagicMock) -> None:
    """Point the adapter's ``async_get_clientsession`` at ``session`` (typed helper)."""

    def _get(_hass: HomeAssistant) -> MagicMock:
        return session

    mp.setattr("custom_components.llm_middleman.backends.langgraph.async_get_clientsession", _get)


# --- parser units --------------------------------------------------------------


def test_extract_text_string_and_blocks() -> None:
    assert _extract_text("hello") == "hello"
    assert _extract_text([{"type": "text", "text": "a"}, {"type": "tool_use"}, "b"]) == "ab"
    assert _extract_text({"unexpected": "shape"}) == ""


@pytest.mark.parametrize(
    ("chunk", "expected"),
    [
        ({"type": "AIMessageChunk", "content": "tok"}, "tok"),
        ({"type": "ai", "content": "whole"}, "whole"),
        ({"type": "tool", "content": "sunny and 21C", "tool_call_id": "call_1"}, None),
        ({"type": "human", "content": "weather?"}, None),
        ({"type": "AIMessageChunk", "content": "", "tool_call_chunks": [{"name": "get_weather"}]}, None),
        ({"content": "untyped"}, None),
    ],
    ids=["ai-chunk", "ai-whole", "tool", "human", "tool-call-chunk", "untyped"],
)
def test_parse_messages_frame_speaks_only_ai_text(chunk: dict[str, Any], expected: str | None) -> None:
    assert _parse_messages_frame(json.dumps([chunk, {"langgraph_node": "agent"}])) == (expected, "agent")


@pytest.mark.parametrize("data", ["{not json", json.dumps({"not": "a tuple"}), json.dumps([1, 2, 3])])
def test_parse_messages_frame_skips_malformed(data: str) -> None:
    assert _parse_messages_frame(data) == (None, None)


def test_parse_updates_frame_reads_interrupt_value() -> None:
    update = _parse_updates_frame(json.dumps({"__interrupt__": [{"value": "Which room?", "id": "i1"}]}), "messages")
    assert (update.interrupted, update.interrupt) == (True, "Which room?")


def test_parse_updates_frame_ignores_malformed_payloads() -> None:
    assert _parse_updates_frame("{not json", "messages").interrupted is False
    assert _parse_updates_frame(json.dumps(["list"]), "messages").tool_calls == []
    assert _parse_updates_frame(json.dumps({"agent": None, "tools": {"messages": "x"}}), "messages").tool_results == []


def test_parse_updates_frame_reads_custom_messages_key() -> None:
    update = _parse_updates_frame(
        json.dumps({"agent": {"msgs": {"type": "ai", "tool_calls": [{"id": "c1", "name": "t", "args": {}}]}}}), "msgs"
    )
    assert update.tool_calls == [("c1", "t", {})]


# --- replaying real captures ------------------------------------------------------


async def test_react_capture_speaks_only_the_answer(hass: HomeAssistant) -> None:
    """Tool-call chunks and the ``tool`` message (its output) are never spoken."""
    deltas, _ = await _turn(_adapter(hass, _FakeServer(_capture("react_tool_call"))))
    assert _text_of(deltas) == _ANSWER


async def test_react_capture_surfaces_tool_call_as_external(hass: HomeAssistant) -> None:
    deltas, _ = await _turn(_adapter(hass, _FakeServer(_capture("react_tool_call"))))
    (call,) = _tool_calls(deltas)
    assert (call.id, call.tool_name, call.tool_args, call.external) == (
        "call_1",
        "get_weather",
        {"city": "Paris"},
        True,
    )


async def test_react_capture_orders_tool_activity_before_reply(hass: HomeAssistant) -> None:
    """call -> result -> a fresh assistant block for the reply (never ends on the result)."""
    deltas, _ = await _turn(_adapter(hass, _FakeServer(_capture("react_tool_call"))))
    roles = [delta.get("role") for delta in deltas if "role" in delta]
    assert roles == ["assistant", "tool_result", "assistant"]


async def test_react_capture_traces_token_usage(hass: HomeAssistant) -> None:
    chat_log = _chat_log()
    await _turn(_adapter(hass, _FakeServer(_capture("react_tool_call"))), chat_log=chat_log)
    # Two model calls in the capture: 11+23 input, 5+7 output tokens.
    chat_log.async_trace.assert_called_once_with({"stats": {"input_tokens": 34, "output_tokens": 12}})


async def test_run_without_usage_traces_nothing(hass: HomeAssistant) -> None:
    chat_log = _chat_log()
    await _turn(_adapter(hass, _FakeServer(_capture("echo_whole_ai_message"))), chat_log=chat_log)
    chat_log.async_trace.assert_not_called()


async def test_whole_ai_message_from_non_streaming_node_is_spoken(hass: HomeAssistant) -> None:
    deltas, _ = await _turn(_adapter(hass, _FakeServer(_capture("echo_whole_ai_message"))))
    assert deltas == [{"role": "assistant"}, {"content": "echo:hello"}]


async def test_heartbeat_comments_during_slow_step_are_ignored(hass: HomeAssistant) -> None:
    deltas, _ = await _turn(_adapter(hass, _FakeServer(_capture("slow_heartbeats"))))
    assert _text_of(deltas) == "Done after a slow step."


async def test_error_event_raises_backend_stream_error(hass: HomeAssistant) -> None:
    with pytest.raises(BackendStreamError, match="TypeError"):
        await _turn(_adapter(hass, _FakeServer(_capture("error_event"))))


@pytest.mark.parametrize("chunk", [1, 3, 4096])
async def test_capture_survives_any_chunking(hass: HomeAssistant, chunk: int) -> None:
    deltas, _ = await _turn(_adapter(hass, _FakeServer(_capture("react_tool_call"), chunk=chunk)))
    assert _text_of(deltas) == _ANSWER


async def test_tools_without_reply_text_raise(hass: HomeAssistant) -> None:
    """A run that ends after its tool result raises, so the entity guard appends the fallback."""
    blob = _events_until(_capture("react_tool_call"), b'{"tools":')  # through the tools update
    with pytest.raises(BackendStreamError, match="no reply text"):
        await _turn(_adapter(hass, _FakeServer(blob)))


async def test_node_filter_speaks_only_matching_node(hass: HomeAssistant) -> None:
    blob = sse_bytes(
        ("messages", json.dumps([{"type": "AIMessageChunk", "content": "chatter "}, {"langgraph_node": "planner"}])),
        ("messages", json.dumps([{"type": "AIMessageChunk", "content": "Answer"}, {"langgraph_node": "agent"}])),
    )
    deltas, _ = await _turn(_adapter(hass, _FakeServer(blob)), options={"response_node_filter": "agent"})
    assert deltas == [{"role": "assistant"}, {"content": "Answer"}]


async def test_run_http_error_raises_backend_stream_error(hass: HomeAssistant) -> None:
    server = _FakeServer()
    server.session.post = MagicMock(return_value=_StreamResp(_FakeStreamContent([]), status=500))
    with pytest.raises(BackendStreamError, match="HTTP 500"):
        await _turn(_adapter(hass, server))


# --- interrupts ---------------------------------------------------------------------


async def test_interrupt_speaks_question_and_requests_follow_up(hass: HomeAssistant) -> None:
    deltas, ctx = await _turn(_adapter(hass, _FakeServer(_capture("interrupt"))))
    assert deltas == [{"role": "assistant"}, {"content": _QUESTION}]
    assert ctx.continue_conversation is True


@pytest.mark.parametrize(
    ("value", "spoken"),
    [("Which room?", "Which room?"), ({"message": "Confirm?"}, "Confirm?"), ({"other": 1}, None)],
    ids=["string", "message-key", "unreadable"],
)
async def test_interrupt_value_shapes(hass: HomeAssistant, value: object, spoken: str | None) -> None:
    blob = sse_bytes(("updates", json.dumps({"__interrupt__": [{"value": value, "id": "i1"}]})))
    deltas, _ = await _turn(_adapter(hass, _FakeServer(blob)))
    assert _text_of(deltas) == (spoken or "The assistant needs more information to continue.")


async def test_turn_after_interrupt_resumes_with_the_reply(hass: HomeAssistant) -> None:
    server = _FakeServer(_capture("interrupt"), _capture("interrupt_resumed"), thread=_thread_json())
    adapter = _adapter(hass, server)
    await _turn(adapter)

    deltas, _ = await _turn(adapter, text="kitchen")

    assert server.body()["command"] == {"resume": "kitchen"}
    assert "input" not in server.body()
    assert _text_of(deltas) == "Turning on the lights in the kitchen."


async def test_resume_is_confirmed_against_the_thread(hass: HomeAssistant) -> None:
    server = _FakeServer(_capture("interrupt"), _capture("echo_whole_ai_message"), thread=_thread_json())
    adapter = _adapter(hass, server)
    await _turn(adapter)
    await _turn(adapter)
    assert [url for method, url, _ in server.calls if method == "GET"] == [
        server.run_urls[0].removesuffix("/runs/stream")
    ]


async def test_stale_resume_mark_sends_normal_input(hass: HomeAssistant) -> None:
    """A thread whose interrupt is gone (cancelled run: interrupts == {}) gets new input."""
    server = _FakeServer(_capture("interrupt"), _capture("echo_whole_ai_message"), thread={"interrupts": {}})
    adapter = _adapter(hass, server)
    await _turn(adapter)

    await _turn(adapter, text="hello")

    assert server.body()["input"]["messages"] == [{"role": "user", "content": "hello"}]


async def test_resume_mark_is_consumed_once(hass: HomeAssistant) -> None:
    server = _FakeServer(
        _capture("interrupt"), _capture("interrupt_resumed"), _capture("echo_whole_ai_message"), thread=_thread_json()
    )
    adapter = _adapter(hass, server)
    for _ in range(3):
        await _turn(adapter)
    assert "input" in server.body()


async def test_deleted_thread_is_not_resumed(hass: HomeAssistant) -> None:
    server = _FakeServer(_capture("interrupt"), _capture("echo_whole_ai_message"), thread=None)
    adapter = _adapter(hass, server)
    await _turn(adapter)
    await _turn(adapter)
    assert "input" in server.body()


async def test_thread_lookup_error_fails_the_turn(hass: HomeAssistant) -> None:
    server = _FakeServer(_capture("interrupt"))
    adapter = _adapter(hass, server)
    await _turn(adapter)
    server.session.get = MagicMock(return_value=_JsonResp({}, status=500))
    with pytest.raises(BackendStreamError, match="HTTP 500"):
        await _turn(adapter)


async def test_device_scope_resume_mark_survives_restart(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    opts = {"memory_scope": "device"}
    first = _FakeServer(_capture("interrupt"))
    await _turn(_adapter(hass, first), options=opts, key="dev-A")
    thread_id = first.run_urls[0].split("/")[-3]
    assert hass_storage["llm_middleman.langgraph.e1.pending_resume"]["data"] == [thread_id]

    second = _FakeServer(_capture("interrupt_resumed"), thread=_thread_json())
    await _turn(_adapter(hass, second), options=opts, key="dev-A", text="kitchen")
    assert second.body()["command"] == {"resume": "kitchen"}


async def test_conversation_scope_resume_mark_is_not_persisted(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    await _turn(_adapter(hass, _FakeServer(_capture("interrupt"))))
    assert "llm_middleman.langgraph.e1.pending_resume" not in hass_storage


# --- request shape --------------------------------------------------------------------


async def test_threaded_run_body(hass: HomeAssistant) -> None:
    server = _FakeServer(_capture("echo_whole_ai_message"))
    await _turn(_adapter(hass, server))
    body = server.body()
    assert body["stream_mode"] == ["messages-tuple", "updates"]
    assert (body["if_not_exists"], body["on_disconnect"], body["multitask_strategy"]) == (
        "create",
        "cancel",
        "rollback",
    )
    assert body["input"] == {"messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.parametrize(("configured", "sent"), [("enqueue", "enqueue"), ("bogus", "rollback")])
async def test_multitask_strategy_option(hass: HomeAssistant, configured: str, sent: str) -> None:
    server = _FakeServer(_capture("echo_whole_ai_message"))
    await _turn(_adapter(hass, server), options={"multitask_strategy": configured})
    assert server.body()["multitask_strategy"] == sent


async def test_threaded_run_sends_prompt_and_ha_context(hass: HomeAssistant) -> None:
    server = _FakeServer(_capture("echo_whole_ai_message"))
    await _turn(
        _adapter(hass, server),
        chat_log=_chat_log("Be nice"),
        device_id="dev-1",
        satellite_id="sat-1",
        context=Context(user_id="user-1"),
    )
    assert server.body()["context"] == {
        "system_prompt": "Be nice",
        "language": "en",
        "device_id": "dev-1",
        "satellite_id": "sat-1",
        "conversation_id": "conv-1",
        "user_id": "user-1",
    }


async def test_context_omits_empty_values(hass: HomeAssistant) -> None:
    server = _FakeServer(_capture("echo_whole_ai_message"))
    await _turn(_adapter(hass, server))
    assert server.body()["context"] == {"language": "en", "conversation_id": "conv-1"}


async def test_stateless_run_body(hass: HomeAssistant) -> None:
    server = _FakeServer(_capture("echo_whole_ai_message"))
    adapter = _adapter(hass, server, assistant_id="my-graph")
    await _turn(adapter, options={"stateless_runs": True, "input_messages_key": "msgs"}, chat_log=_chat_log("Be nice"))

    assert server.run_urls == [f"{_BASE_URL}/runs/stream"]
    body = server.body()
    assert body["assistant_id"] == "my-graph"
    # No state accumulates on a stateless run, so the prompt leads the input (never twice).
    assert body["input"]["msgs"] == [{"role": "system", "content": "Be nice"}, {"role": "user", "content": "hi"}]
    assert "system_prompt" not in body["context"]
    assert "if_not_exists" not in body
    assert "multitask_strategy" not in body


async def test_configured_prompt_reaches_context_rendered(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """The agent's CONF_PROMPT template arrives rendered via the real provide-llm-data path."""
    server = _FakeServer(_capture("echo_whole_ai_message"))
    entity = build_entity(hass, _adapter(hass, server), {CONF_PROMPT: "Hi {{ 'x' | upper }}"})

    await entity._async_handle_message(_input(), mock_chat_log)

    system_prompt = server.body()["context"]["system_prompt"]
    assert system_prompt.startswith("Hi X\n")
    assert all(message["role"] == "user" for message in server.body()["input"]["messages"])


@pytest.mark.parametrize(
    ("conn", "expected"),
    [
        ({"api_key": "sk-1"}, {"x-api-key": "sk-1"}),
        ({"api_key": "sk-1", "auth_scheme": "x_api_key"}, {"x-api-key": "sk-1"}),
        ({"api_key": "sk-1", "auth_scheme": "bearer"}, {"Authorization": "Bearer sk-1"}),
        ({"api_key": None, "auth_scheme": "bearer"}, {}),
    ],
    ids=["legacy-default", "x-api-key", "bearer", "no-key"],
)
async def test_auth_headers(hass: HomeAssistant, conn: dict[str, Any], expected: dict[str, str]) -> None:
    server = _FakeServer(_capture("echo_whole_ai_message"))
    await _turn(_adapter(hass, server, **conn))
    assert server.run_calls[0][1]["headers"] == {"Content-Type": "application/json", **expected}


# --- deterministic threads -------------------------------------------------------------


async def test_thread_id_is_stable_across_releases(hass: HomeAssistant) -> None:
    """uuid5(namespace, "<entry_id>:<key>") — changing it would orphan every user's thread."""
    server = _FakeServer(_capture("echo_whole_ai_message"))
    await _turn(_adapter(hass, server), key="dev-A")
    assert server.run_urls == [f"{_BASE_URL}/threads/{_expected_thread('e1', 'dev-A')}/runs/stream"]


def _expected_thread(entry_id: str, key: str) -> str:
    namespace = uuid.UUID("75e17f5a-ab70-5736-83d6-5c5f8ce93dca")
    return str(uuid.uuid5(namespace, f"{entry_id}:{key}"))


async def test_threads_differ_per_session_key_and_entry(hass: HomeAssistant) -> None:
    server = _FakeServer(*[_capture("echo_whole_ai_message")] * 3)
    await _turn(_adapter(hass, server), key="dev-A")
    await _turn(_adapter(hass, server), key="dev-B")
    await _turn(_adapter(hass, server, entry_id="e2"), key="dev-A")
    assert len(set(server.run_urls)) == 3


async def test_no_thread_creation_request_is_made(hass: HomeAssistant) -> None:
    """``if_not_exists: create`` replaces POST /threads (and the old 404 retry)."""
    server = _FakeServer(_capture("echo_whole_ai_message"))
    await _turn(_adapter(hass, server))
    assert [url for _, url, _ in server.calls] == server.run_urls


async def test_legacy_persisted_thread_is_still_used(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    hass_storage["llm_middleman.langgraph.e1"] = {
        "version": 1,
        "key": "llm_middleman.langgraph.e1",
        "data": {"dev-A": "t-legacy"},
    }
    server = _FakeServer(_capture("echo_whole_ai_message"), _capture("echo_whole_ai_message"))
    adapter = _adapter(hass, server)
    await _turn(adapter, options={"memory_scope": "device"}, key="dev-A")
    await _turn(adapter, options={"memory_scope": "device"}, key="dev-B")
    assert server.run_urls == [
        f"{_BASE_URL}/threads/t-legacy/runs/stream",
        f"{_BASE_URL}/threads/{_expected_thread('e1', 'dev-B')}/runs/stream",
    ]


async def test_legacy_device_thread_adopted_once_by_first_agent(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    # Device keys became per agent; the first agent on the device keeps the old shared
    # thread, a sibling agent gets its own instead of sharing it.
    hass_storage["llm_middleman.langgraph.e1"] = {"version": 1, "data": {"dev-A": "t-legacy"}}
    server = _FakeServer(_capture("echo_whole_ai_message"), _capture("echo_whole_ai_message"))
    adapter = _adapter(hass, server)
    await _turn(adapter, options={"memory_scope": "device"}, key="sub-1:dev-A", legacy_key="dev-A")
    await _turn(adapter, options={"memory_scope": "device"}, key="sub-2:dev-A", legacy_key="dev-A")

    assert server.run_urls == [
        f"{_BASE_URL}/threads/t-legacy/runs/stream",
        f"{_BASE_URL}/threads/{_expected_thread('e1', 'sub-2:dev-A')}/runs/stream",
    ]
    assert hass_storage["llm_middleman.langgraph.e1"]["data"] == {"sub-1:dev-A": "t-legacy"}


# --- cancellation ------------------------------------------------------------------------


_METADATA = b'event: metadata\r\ndata: {"run_id":"run-1","attempt":1}\r\n\r\n'


async def _cancel_mid_run(hass: HomeAssistant, server: _FakeServer, content: _HangingContent, **turn: Any) -> None:
    task = asyncio.create_task(_turn(_adapter(hass, server), **turn))
    await asyncio.wait_for(content.started.wait(), timeout=2)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await hass.async_block_till_done()


async def test_cancelled_turn_cancels_the_server_run(hass: HomeAssistant) -> None:
    content = _HangingContent(_METADATA)
    server = _FakeServer(content)
    await _cancel_mid_run(hass, server, content)
    thread_id = _expected_thread("e1", "conv-1")
    assert server.cancel_urls == [f"{_BASE_URL}/threads/{thread_id}/runs/run-1/cancel"]


async def test_cancel_request_failure_is_swallowed(hass: HomeAssistant) -> None:
    content = _HangingContent(_METADATA)
    server = _FakeServer(content)
    server.cancel_exc = aiohttp.ClientConnectionError("gone")
    await _cancel_mid_run(hass, server, content)
    assert len(server.cancel_urls) == 1


async def test_stateless_cancel_relies_on_disconnect(hass: HomeAssistant) -> None:
    content = _HangingContent(_METADATA)
    server = _FakeServer(content)
    await _cancel_mid_run(hass, server, content, options={"stateless_runs": True})
    assert server.cancel_urls == []


async def test_completed_run_is_not_cancelled(hass: HomeAssistant) -> None:
    server = _FakeServer(_capture("react_tool_call"))
    await _turn(_adapter(hass, server))
    await hass.async_block_till_done()
    assert server.cancel_urls == []


async def test_last_run_id_is_exposed_for_diagnostics(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _FakeServer(_capture("interrupt")))
    await _turn(adapter)
    assert adapter.diagnostics() == {
        "last_run_id": "01a1700a-40ad-73a7-9be6-d5cffb43c0b9",
        "threads_awaiting_resume": 1,
        "legacy_thread_map_entries": 0,
    }


# --- through the entity (tool loop) ---------------------------------------------------------


async def test_turn_with_tool_activity_posts_exactly_once(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """External tool results must not re-enter the entity's HA tool loop (one POST per turn)."""
    server = _FakeServer(_capture("react_tool_call"), _capture("react_tool_call"))
    entity = build_entity(hass, _adapter(hass, server), {})

    result = await entity._async_handle_message(_input(), mock_chat_log)

    assert len(server.run_calls) == 1
    assert result.response.speech["plain"]["speech"] == _ANSWER


async def test_tool_activity_is_recorded_in_chat_log(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    entity = build_entity(hass, _adapter(hass, _FakeServer(_capture("react_tool_call"))), {})

    await entity._async_handle_message(_input(), mock_chat_log)

    call, result, reply = mock_chat_log.content[-3:]
    assert isinstance(call, conversation.AssistantContent)
    assert call.tool_calls is not None
    assert call.tool_calls[0].external is True
    assert isinstance(result, conversation.ToolResultContent)
    assert result.tool_call_id == "call_1"
    assert isinstance(reply, conversation.AssistantContent)
    assert reply.content == _ANSWER


async def test_tools_without_reply_post_once_and_speak_fallback(
    hass: HomeAssistant, mock_chat_log: MockChatLog
) -> None:
    blob = _events_until(_capture("react_tool_call"), b'{"tools":')
    server = _FakeServer(blob, blob)
    entity = build_entity(hass, _adapter(hass, server), {})

    result = await entity._async_handle_message(_input(), mock_chat_log)

    assert len(server.run_calls) == 1
    assert result.response.speech["plain"]["speech"] == ERROR_MESSAGE


async def test_interrupt_through_entity_keeps_listening(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    entity = build_entity(hass, _adapter(hass, _FakeServer(_capture("interrupt"))), {})

    result = await entity._async_handle_message(_input(), mock_chat_log)

    assert result.continue_conversation is True
    assert result.response.speech["plain"]["speech"] == _QUESTION


# --- connection validation --------------------------------------------------------------------


def _probe_session(*, get: Any = None, post: Any = None) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(return_value=get) if not isinstance(get, Exception) else MagicMock(side_effect=get)
    session.post = MagicMock(return_value=post) if not isinstance(post, Exception) else MagicMock(side_effect=post)
    return session


async def _validate(hass: HomeAssistant, session: MagicMock, **data: Any) -> None:
    with pytest.MonkeyPatch.context() as mp:
        _patch_client_session(mp, session)
        await LangGraphAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, **data})


async def test_validate_graph_id_searches_assistants(hass: HomeAssistant) -> None:
    session = _probe_session(post=_JsonResp([{"assistant_id": "a1", "graph_id": "agent"}]))
    await _validate(hass, session, assistant_id="agent", api_key="sk-1")
    assert session.post.call_args.args[0] == f"{_BASE_URL}/assistants/search"
    assert json.loads(session.post.call_args.kwargs["data"]) == {"graph_id": "agent", "limit": 1}
    assert session.post.call_args.kwargs["headers"]["x-api-key"] == "sk-1"


async def test_validate_uuid_gets_assistant(hass: HomeAssistant) -> None:
    assistant = "fe096781-5601-53d2-b2f6-0d3403f7e9ca"
    session = _probe_session(get=_JsonResp({"assistant_id": assistant}))
    await _validate(hass, session, assistant_id=assistant, api_key="sk-1", auth_scheme="bearer")
    assert session.get.call_args.args[0] == f"{_BASE_URL}/assistants/{assistant}"
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-1"


@pytest.mark.parametrize(
    "post",
    [_JsonResp({"detail": "Graph 'nope' not found"}, status=404), _JsonResp([])],
    ids=["404", "empty-search"],
)
async def test_validate_unknown_graph_is_assistant_not_found(hass: HomeAssistant, post: _JsonResp) -> None:
    with pytest.raises(AssistantNotFoundError):
        await _validate(hass, _probe_session(post=post), assistant_id="nope")


@pytest.mark.parametrize(
    ("post", "error"),
    [
        (_JsonResp({}, status=401), BackendAuthError),
        (_JsonResp({}, status=500), BackendConnectionError),
        (aiohttp.ClientConnectionError("no route"), BackendConnectionError),
        (_JsonResp(ValueError("not json")), BackendConnectionError),
    ],
    ids=["auth", "status", "transport", "bad-json"],
)
async def test_validate_failures(hass: HomeAssistant, post: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        await _validate(hass, _probe_session(post=post))
