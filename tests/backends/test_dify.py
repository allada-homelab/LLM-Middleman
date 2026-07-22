"""Tests for the Dify adapter (LLMM-020).

Raw SSE bytes are driven through the **real** ``_sse`` parser + the adapter via the
conftest harness (arbitrary chunk boundaries, CRLF). ``session_key -> conversation_id``
mapping / persistence is exercised against a real ``helpers.storage.Store`` backed by the
``hass_storage`` fixture, mirroring ``test_langgraph.py``.

Wire facts (Dify ``POST /chat-messages``, ``response_mode: streaming``): ``agent_message``
(Agent apps) and ``message`` (Chatbot/Chatflow) both carry ``answer`` deltas + a
``conversation_id``; ``message_end`` terminates; ``error`` ends an HTTP-200 stream with a
failure; ``ping``/``agent_thought``/``message_file``/``tts_*`` are ignored.
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import MagicMock

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant

from custom_components.llm_middleman.backends.base import (
    BackendAuthError,
    BackendConnectionError,
    BackendStreamError,
    DeltaStream,
    TurnContext,
)
from custom_components.llm_middleman.backends.dify import DifyAdapter
from tests.conftest import FakeStreamResponse, chunk_bytes, sse_bytes

_BASE_URL = "https://api.dify.ai/v1"
_CONN: dict[str, Any] = {"base_url": _BASE_URL, "api_key": "app-key", "entry_id": "e1"}


# --- fakes ------------------------------------------------------------------


class _InfoResp:
    """JSON response CM for the ``GET /info`` validation probe."""

    def __init__(self, status: int, payload: dict[str, Any] | None = None) -> None:
        self.status = status
        self._payload = payload if payload is not None else {}

    async def json(self) -> dict[str, Any]:
        return self._payload

    async def __aenter__(self) -> _InfoResp:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _input(text: str = "hi") -> conversation.ConversationInput:
    return conversation.ConversationInput(
        text=text,
        context=Context(),
        conversation_id="conv-1",
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="agent-1",
    )


def _chat_log() -> conversation.ChatLog:
    # The adapter deletes chat_log immediately (server-side history); shape is irrelevant.
    return cast("conversation.ChatLog", MagicMock())


def _adapter(hass: HomeAssistant, session: MagicMock, conn: dict[str, Any] | None = None) -> DifyAdapter:
    return DifyAdapter(hass, cast("aiohttp.ClientSession", session), conn if conn is not None else _CONN)


def _msg(
    answer: str,
    *,
    event: str = "agent_message",
    cid: str = "conv-abc",
    task_id: str = "task-1",
) -> tuple[str, str]:
    """A Dify content event carrying an ``answer`` delta + ids."""
    return (
        event,
        json.dumps({"answer": answer, "task_id": task_id, "message_id": "m-1", "conversation_id": cid}),
    )


def _end(*, cid: str = "conv-abc", task_id: str = "task-1") -> tuple[str, str]:
    return ("message_end", json.dumps({"metadata": {}, "conversation_id": cid, "task_id": task_id}))


def _session(blob: bytes, *, chunk: int | list[int] = 1, status: int = 200, text: str = "") -> MagicMock:
    """Session whose single ``/chat-messages`` POST streams ``blob`` in ``chunk`` pieces."""
    session = MagicMock()
    session.post = MagicMock(return_value=FakeStreamResponse(chunk_bytes(blob, chunk), status=status, text=text))
    return session


async def _collect(gen: DeltaStream) -> list[conversation.AssistantContentDeltaDict]:
    return [delta async for delta in gen]


def _text_of(deltas: list[conversation.AssistantContentDeltaDict]) -> str:
    return "".join(delta.get("content") or "" for delta in deltas)


async def _run(
    hass: HomeAssistant,
    blob: bytes,
    *,
    chunk: int | list[int] = 1,
    status: int = 200,
    text: str = "",
    options: dict[str, Any] | None = None,
) -> list[conversation.AssistantContentDeltaDict]:
    adapter = _adapter(hass, _session(blob, chunk=chunk, status=status, text=text))
    ctx = TurnContext(options=options or {}, memory_key="k")
    return await _collect(adapter.stream_turn(_chat_log(), _input(), ctx))


def _patch_session(mp: pytest.MonkeyPatch, session: MagicMock) -> None:
    def _get(_hass: HomeAssistant) -> MagicMock:
        return session

    mp.setattr("custom_components.llm_middleman.backends.dify.async_get_clientsession", _get)


# --- classvars --------------------------------------------------------------


def test_adapter_classvars() -> None:
    assert DifyAdapter.backend_type == "dify"
    assert DifyAdapter.supports_ha_tools is False
    assert DifyAdapter.supports_memory_scope is True


# --- streaming happy path ---------------------------------------------------


@pytest.mark.parametrize("event", ["agent_message", "message"])
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
async def test_happy_path_streams_both_event_types(hass: HomeAssistant, event: str, newline: bytes) -> None:
    # Both Agent (agent_message) and Chatbot/Chatflow (message) deltas stream identically.
    blob = sse_bytes(_msg("Hello ", event=event), _msg("world", event=event), _end(), newline=newline)
    deltas = await _run(hass, blob, chunk=5)
    assert deltas[0] == {"role": "assistant"}
    assert _text_of(deltas) == "Hello world"


async def test_message_end_terminates_stream(hass: HomeAssistant) -> None:
    # Content after message_end is never emitted (message_end is terminal).
    blob = sse_bytes(_msg("Hi"), _end(), _msg("after-end"))
    assert _text_of(await _run(hass, blob, chunk=6)) == "Hi"


async def test_ignored_events_produce_no_content(hass: HomeAssistant) -> None:
    blob = sse_bytes(
        ("ping", '{"event":"ping"}'),
        ("agent_thought", '{"thought":"reasoning"}'),
        ("message_file", '{"type":"image"}'),
        ("tts_message", '{"audio":"..."}'),
        _msg("answer"),
        _end(),
    )
    assert await _run(hass, blob, chunk=7) == [{"role": "assistant"}, {"content": "answer"}]


async def test_in_stream_error_event_raises(hass: HomeAssistant) -> None:
    # An error event ends an otherwise-200 stream with a failure.
    blob = sse_bytes(_msg("partial"), ("error", '{"status":400,"code":"boom","message":"bad"}'))
    with pytest.raises(BackendStreamError):
        await _run(hass, blob, chunk=5)


# --- <think> reasoning stripped from spoken output --------------------------
# Reasoning models (e.g. via LiteLLM/llama-swap) inline chain-of-thought as
# <think>…</think> in the answer; agent-chat apps have no post-processing node,
# so the raw reasoning reaches the answer stream. HA speaks every content delta
# aloud, so the adapter must strip think blocks before yielding — keeping only
# the final prose. Blocks can span multiple deltas and the tags can split across
# chunk boundaries.


async def test_think_blocks_stripped_from_answer(hass: HomeAssistant) -> None:
    # Multiple <think> blocks (one per ReAct step) then the real answer; only the
    # final prose is spoken. Tiny chunks split the tags across raw byte boundaries.
    blob = sse_bytes(
        _msg("<think>\nLet me check the time.\n</think>"),
        _msg("<think>\nGot it.\n</think>"),
        _msg("It's twelve thirty-four PM, sir."),
        _end(),
    )
    deltas = await _run(hass, blob, chunk=3)
    assert _text_of(deltas) == "It's twelve thirty-four PM, sir."
    # role-first invariant: emitted exactly once, and only once real content exists.
    assert deltas[0] == {"role": "assistant"}
    assert sum(1 for d in deltas if d.get("role")) == 1


async def test_think_tags_split_across_deltas_are_stripped(hass: HomeAssistant) -> None:
    # Opening/closing tags arrive split across separate answer deltas.
    blob = sse_bytes(
        _msg("<th"),
        _msg("ink>reasoning"),
        _msg(" here</th"),
        _msg("ink>real answer"),
        _end(),
    )
    assert _text_of(await _run(hass, blob, chunk=4)) == "real answer"


async def test_answer_without_think_passes_through(hass: HomeAssistant) -> None:
    blob = sse_bytes(_msg("Plain answer "), _msg("no tags"), _end())
    assert _text_of(await _run(hass, blob, chunk=5)) == "Plain answer no tags"


async def test_unclosed_think_emits_no_content(hass: HomeAssistant) -> None:
    # A truncated stream with an open <think> and no close must never speak the
    # partial reasoning (safe default: drop it entirely).
    blob = sse_bytes(_msg("<think>\nthinking but cut off"), _end())
    assert _text_of(await _run(hass, blob, chunk=4)) == ""


async def test_literal_angle_bracket_text_preserved(hass: HomeAssistant) -> None:
    # A bare "<" that is not a think tag must survive (no over-eager swallowing).
    blob = sse_bytes(_msg("price is < 5 dollars"), _end())
    assert _text_of(await _run(hass, blob, chunk=3)) == "price is < 5 dollars"


# --- request shape ----------------------------------------------------------


async def test_request_shape_first_turn_omits_conversation_id(hass: HomeAssistant) -> None:
    captured: dict[str, Any] = {}

    def handler(url: str, **kwargs: Any) -> FakeStreamResponse:
        captured["url"] = url
        captured["data"] = kwargs["data"]
        captured["headers"] = kwargs["headers"]
        return FakeStreamResponse(chunk_bytes(sse_bytes(_msg("ok"), _end()), 8))

    session = MagicMock()
    session.post = MagicMock(side_effect=handler)
    adapter = _adapter(hass, session)
    await _collect(adapter.stream_turn(_chat_log(), _input("hello"), TurnContext(options={}, memory_key="k")))

    assert captured["url"] == f"{_BASE_URL}/chat-messages"
    body = json.loads(cast("str", captured["data"]))
    assert body["query"] == "hello"
    assert body["inputs"] == {}
    assert body["response_mode"] == "streaming"
    assert body["user"] == "home-assistant"
    assert body["auto_generate_name"] is False
    assert "conversation_id" not in body
    assert captured["headers"]["Authorization"] == "Bearer app-key"


# --- conversation-id capture / echo / persistence ---------------------------


async def test_second_turn_echoes_captured_conversation_id(hass: HomeAssistant) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(_url: str, **kwargs: Any) -> FakeStreamResponse:
        bodies.append(json.loads(cast("str", kwargs["data"])))
        return FakeStreamResponse(chunk_bytes(sse_bytes(_msg("ok", cid="conv-xyz"), _end(cid="conv-xyz")), 8))

    session = MagicMock()
    session.post = MagicMock(side_effect=handler)
    adapter = _adapter(hass, session)
    ctx = TurnContext(options={}, memory_key="k")
    await _collect(adapter.stream_turn(_chat_log(), _input(), ctx))
    await _collect(adapter.stream_turn(_chat_log(), _input(), ctx))

    assert "conversation_id" not in bodies[0]  # first turn: no id
    assert bodies[1]["conversation_id"] == "conv-xyz"  # second turn: echoes captured id


async def test_conversation_scope_not_persisted(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    await _run(hass, sse_bytes(_msg("ok", cid="conv-c"), _end(cid="conv-c")), chunk=8)
    assert "llm_middleman.dify.e1" not in hass_storage


async def test_device_scope_persists_conversation_id(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    blob = sse_bytes(_msg("ok", cid="conv-dev"), _end(cid="conv-dev"))
    adapter = _adapter(hass, _session(blob, chunk=8))
    await _collect(
        adapter.stream_turn(_chat_log(), _input(), TurnContext(options={"memory_scope": "device"}, memory_key="dev-A"))
    )
    assert hass_storage["llm_middleman.dify.e1"]["data"] == {"dev-A": "conv-dev"}


async def test_device_map_persists_across_restart(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    blob = sse_bytes(_msg("ok", cid="conv-dev"), _end(cid="conv-dev"))

    adapter1 = _adapter(hass, _session(blob, chunk=8))
    await _collect(
        adapter1.stream_turn(_chat_log(), _input(), TurnContext(options={"memory_scope": "device"}, memory_key="dev-A"))
    )
    assert hass_storage["llm_middleman.dify.e1"]["data"] == {"dev-A": "conv-dev"}

    # "Restart": a fresh adapter (same entry) reloads the map and echoes the stored id.
    bodies: list[dict[str, Any]] = []

    def handler(_url: str, **kwargs: Any) -> FakeStreamResponse:
        bodies.append(json.loads(cast("str", kwargs["data"])))
        return FakeStreamResponse(chunk_bytes(blob, 8))

    session2 = MagicMock()
    session2.post = MagicMock(side_effect=handler)
    adapter2 = _adapter(hass, session2)
    await _collect(
        adapter2.stream_turn(_chat_log(), _input(), TurnContext(options={"memory_scope": "device"}, memory_key="dev-A"))
    )
    assert bodies[0]["conversation_id"] == "conv-dev"


# Dify has shipped >1 wire shape for a stale/deleted conversation_id 404: the documented
# ``conversation_not_exists`` code and the live ``not_found`` code + "Conversation Not Exists"
# message. Both must trigger the drop-and-recreate, or the stale id wedges the agent forever.
@pytest.mark.parametrize(
    "not_found_body",
    [
        '{"code":"conversation_not_exists","message":"gone"}',
        '{"code":"not_found","message":"Conversation Not Exists. You have requested this URI ...","status":404}',
    ],
)
async def test_stale_conversation_id_dropped_and_retried_once(hass: HomeAssistant, not_found_body: str) -> None:
    state = {"served": 0}
    bodies: list[dict[str, Any]] = []

    def handler(_url: str, **kwargs: Any) -> FakeStreamResponse:
        body = json.loads(cast("str", kwargs["data"]))
        bodies.append(body)
        if body.get("conversation_id") == "old-id":
            # The echoed id was deleted server-side: pre-stream 404 before any bytes.
            return FakeStreamResponse([], status=404, text=not_found_body)
        cid = "new-id" if state["served"] else "old-id"
        state["served"] += 1
        return FakeStreamResponse(chunk_bytes(sse_bytes(_msg("recovered", cid=cid), _end(cid=cid)), 8))

    session = MagicMock()
    session.post = MagicMock(side_effect=handler)
    adapter = _adapter(hass, session)
    ctx = TurnContext(options={}, memory_key="k")

    await _collect(adapter.stream_turn(_chat_log(), _input(), ctx))  # turn 1 -> captures old-id
    deltas = await _collect(adapter.stream_turn(_chat_log(), _input(), ctx))  # turn 2 -> 404 -> retry

    assert bodies[1]["conversation_id"] == "old-id"  # stale id echoed
    assert "conversation_id" not in bodies[2]  # retried once without id
    assert _text_of(deltas) == "recovered"


# --- pre-stream errors ------------------------------------------------------


async def test_pre_stream_auth_error_raises(hass: HomeAssistant) -> None:
    with pytest.raises(BackendAuthError):
        await _run(hass, b"", status=401, chunk=1)


async def test_pre_stream_non_2xx_raises_connection_error(hass: HomeAssistant) -> None:
    with pytest.raises(BackendConnectionError):
        await _run(hass, b"", status=500, chunk=1)


# --- best-effort stop on cancel ---------------------------------------------


async def test_stop_fired_on_cancel_midstream(hass: HomeAssistant) -> None:
    posted: list[str] = []

    def handler(url: str, **_kwargs: Any) -> FakeStreamResponse:
        posted.append(url)
        if url.endswith("/stop"):
            return FakeStreamResponse([b""])
        # No message_end: the generator stays suspended at the content yield.
        return FakeStreamResponse(chunk_bytes(sse_bytes(_msg("Hi", task_id="task-9")), 8))

    session = MagicMock()
    session.post = MagicMock(side_effect=handler)
    adapter = _adapter(hass, session)
    gen = adapter.stream_turn(_chat_log(), _input(), TurnContext(options={}, memory_key="k"))

    assert await anext(gen) == {"role": "assistant"}
    assert await anext(gen) == {"content": "Hi"}
    await gen.aclose()  # GeneratorExit mid-stream, task_id already captured
    await hass.async_block_till_done()

    assert f"{_BASE_URL}/chat-messages/task-9/stop" in posted


async def test_no_stop_on_cancel_before_task_id_seen(hass: HomeAssistant) -> None:
    posted: list[str] = []
    # A content event with no task_id, and no message_end: the generator suspends at
    # the content yield with state["task_id"] still None.
    no_task_id_event = ("agent_message", json.dumps({"answer": "Hi", "message_id": "m-1"}))

    def handler(url: str, **_kwargs: Any) -> FakeStreamResponse:
        posted.append(url)
        return FakeStreamResponse(chunk_bytes(sse_bytes(no_task_id_event), 8))

    session = MagicMock()
    session.post = MagicMock(side_effect=handler)
    adapter = _adapter(hass, session)
    gen = adapter.stream_turn(_chat_log(), _input(), TurnContext(options={}, memory_key="k"))

    assert await anext(gen) == {"role": "assistant"}
    assert await anext(gen) == {"content": "Hi"}
    await gen.aclose()  # GeneratorExit mid-stream, no task_id ever captured
    await hass.async_block_till_done()

    assert posted == [f"{_BASE_URL}/chat-messages"]  # the turn ran; no /stop fired


# --- connection validation --------------------------------------------------


@pytest.mark.parametrize("mode", ["chat", "agent-chat", "advanced-chat"])
async def test_validate_accepts_chat_modes(hass: HomeAssistant, mode: str) -> None:
    session = MagicMock()
    session.get = MagicMock(return_value=_InfoResp(200, {"mode": mode, "name": "app"}))
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        await DifyAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, "api_key": "app-key"})
    assert session.get.call_args.args[0] == f"{_BASE_URL}/info"
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer app-key"


async def test_validate_rejects_non_chat_mode(hass: HomeAssistant) -> None:
    session = MagicMock()
    session.get = MagicMock(return_value=_InfoResp(200, {"mode": "workflow"}))
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        with pytest.raises(BackendConnectionError):
            await DifyAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, "api_key": "k"})


async def test_validate_auth_error(hass: HomeAssistant) -> None:
    session = MagicMock()
    session.get = MagicMock(return_value=_InfoResp(401))
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        with pytest.raises(BackendAuthError):
            await DifyAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, "api_key": "bad"})


async def test_validate_transport_error_is_connection_error(hass: HomeAssistant) -> None:
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientConnectionError("no route"))
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        with pytest.raises(BackendConnectionError):
            await DifyAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, "api_key": "k"})
