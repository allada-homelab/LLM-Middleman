"""Tests for the Dify adapter (LLMM-020, tier-1 LLMM-021).

Raw SSE bytes are driven through the **real** ``_sse`` parser + the adapter via the
conftest harness (arbitrary chunk boundaries, CRLF), with a real ``ChatLog`` so trace
records land in a real ``ConversationTrace``. ``session_key -> conversation_id`` mapping /
persistence is exercised against a real ``helpers.storage.Store`` backed by the
``hass_storage`` fixture, mirroring ``test_langgraph.py``.

Wire facts, per the Service API spec (https://docs.dify.ai/en/api-reference/openapi_service.json,
``POST /chat-messages`` → ``text/event-stream``): every event except ``ping`` is a bare
``data: {json}`` frame with the event name INSIDE the JSON — no ``event:`` line; ``ping`` is
``event: ping`` with no data. Apart from ``ping`` every event carries ``conversation_id``,
``message_id`` and ``created_at``; all but ``error`` also carry ``task_id``. Outcomes:
Chatbot ``message``… → ``message_end``; (Legacy) Agent ``agent_message``… (+ ``agent_thought``)
→ [Agent: one closing ``message`` with the complete answer] → ``message_end``; Chatflow
``workflow_started`` → ``node_*`` + ``message``… → ``message_end`` + ``workflow_finished``
(success) | ``workflow_finished(failed)`` + ``error`` (failure) | ``human_input_required`` +
``workflow_paused`` (pause, stream ends).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import MagicMock

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.components.conversation import trace
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_middleman.backends.base import (
    BackendAuthError,
    BackendConnectionError,
    BackendStatusError,
    BackendStreamError,
    TurnContext,
)
from custom_components.llm_middleman.backends.dify import PAUSED_NOTICE, DifyAdapter, DifyAppError
from custom_components.llm_middleman.const import ERROR_MESSAGE
from tests.conftest import (
    FakeJsonResponse,
    FakeStreamResponse,
    HangingStreamContent,
    MockChatLog,
    build_entity,
    chunk_bytes,
    collect,
    make_input,
    sse_bytes,
    text_of,
)

_BASE_URL = "https://api.dify.ai/v1"
_CONN: dict[str, Any] = {"base_url": _BASE_URL, "api_key": "app-key", "entry_id": "e1"}
_CHAT_URL = f"{_BASE_URL}/chat-messages"
_PARAMS_URL = f"{_BASE_URL}/parameters"

# Realistic ids in the spec's shapes (UUIDs; created_at in Unix epoch seconds).
_TASK = "c3800678-a077-43df-a102-53f23ed20b88"
_MSG = "9da23599-e713-473b-982c-4328d4f5c78a"
_CONV = "45701982-8118-4bc5-8e9b-64562b4555f2"
_RUN = "fb47b2e6-5e43-4f90-be01-d5c5a088d156"
_CREATED = 1705407629
# The spec's ``Usage`` example (POST /chat-messages blocking response).
_USAGE: dict[str, Any] = {
    "prompt_tokens": 1033,
    "prompt_unit_price": "0.001",
    "prompt_price_unit": "0.001",
    "prompt_price": "0.0010330",
    "completion_tokens": 128,
    "completion_unit_price": "0.002",
    "completion_price_unit": "0.001",
    "completion_price": "0.0002560",
    "total_tokens": 1161,
    "total_price": "0.0012890",
    "currency": "USD",
    "latency": 0.7682376249867957,
}


# --- fakes ------------------------------------------------------------------


def _chat_log(hass: HomeAssistant, extra_system_prompt: str | None = None) -> conversation.ChatLog:
    return conversation.ChatLog(hass, "conv-1", extra_system_prompt=extra_system_prompt)


def _adapter(hass: HomeAssistant, session: MagicMock) -> DifyAdapter:
    return DifyAdapter(hass, cast("aiohttp.ClientSession", session), _CONN)


# Keep-alive exactly as Dify sends it: an ``event:`` line with no ``data:`` (dispatches nothing).
_PING = b"event: ping\n\n"


def _ev(event: str, *, cid: str = _CONV, task_id: str = _TASK, **fields: Any) -> dict[str, Any]:
    """A Dify stream event: the event name rides INSIDE the JSON, with the common fields."""
    return {
        "event": event,
        "task_id": task_id,
        "message_id": _MSG,
        "conversation_id": cid,
        "created_at": _CREATED,
        **fields,
    }


def _msg(answer: str, *, event: str = "agent_message", cid: str = _CONV, task_id: str = _TASK) -> dict[str, Any]:
    """A Dify content event carrying an ``answer`` delta."""
    return _ev(event, cid=cid, task_id=task_id, answer=answer)


def _end(*, cid: str = _CONV, task_id: str = _TASK, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"usage": usage if usage is not None else _USAGE, "retriever_resources": []}
    return _ev("message_end", cid=cid, task_id=task_id, id=_MSG, metadata=metadata)


def _error(code: str = "completion_request_error", message: str = "Completion request failed.") -> dict[str, Any]:
    """An in-stream error event: per the spec it is the one event WITHOUT ``task_id``."""
    return {
        "event": "error",
        "message_id": _MSG,
        "conversation_id": _CONV,
        "created_at": _CREATED,
        "status": 400,
        "code": code,
        "message": message,
    }


def _wf(event: str, **data: Any) -> dict[str, Any]:
    """A Chatflow workflow/node event: payload nested under ``data`` + top-level run id."""
    return _ev(event, workflow_run_id=_RUN, data=data)


_LLM_NODE = {"id": "ne_001", "node_id": "node_llm_1", "node_type": "llm", "title": "LLM", "index": 1}
_WF_STARTED = _wf("workflow_started", id=_RUN, workflow_id="wf_def456", inputs={}, created_at=_CREATED)
_NODE_STARTED = _wf("node_started", **_LLM_NODE, created_at=_CREATED)


def _reasoning(text: str, *, is_final: bool = False) -> dict[str, Any]:
    return _ev(
        "reasoning_chunk",
        data={"message_id": _MSG, "reasoning": text, "node_id": "node_llm_1", "is_final": is_final},
    )


def _dify_sse(*frames: dict[str, Any] | bytes, newline: bytes = b"\n") -> bytes:
    """Build a Dify wire blob: each payload as a bare ``data: {json}`` frame; raw bytes verbatim."""
    out = bytearray()
    for frame in frames:
        if isinstance(frame, bytes):
            out += frame.replace(b"\n", newline)
        else:
            out += b"data: " + json.dumps(frame).encode() + newline + newline
    return bytes(out)


def _session(blob: bytes, *, chunk: int | list[int] = 1, status: int = 200, text: str = "") -> MagicMock:
    """Session whose single ``/chat-messages`` POST streams ``blob`` in ``chunk`` pieces."""
    session = MagicMock()
    session.post = MagicMock(return_value=FakeStreamResponse(chunk_bytes(blob, chunk), status=status, text=text))
    return session


class _Recorder:
    """Session handler serving ``/chat-messages`` replies in order and recording each POST."""

    def __init__(self, *replies: FakeStreamResponse) -> None:
        self._replies = list(replies)
        self.urls: list[str] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> FakeStreamResponse:
        self.urls.append(url)
        self.bodies.append(json.loads(cast("str", kwargs["data"])))
        if url.endswith("/stop"):
            return FakeStreamResponse([b""])
        return self._replies.pop(0)

    def session(self) -> MagicMock:
        session = MagicMock()
        session.post = MagicMock(side_effect=self)
        return session

    def stop_bodies(self) -> list[dict[str, Any]]:
        return [body for url, body in zip(self.urls, self.bodies, strict=True) if url.endswith("/stop")]


def _reply(blob: bytes, **kwargs: Any) -> FakeStreamResponse:
    return FakeStreamResponse(chunk_bytes(blob, 8), **kwargs)


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
    return await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))


async def _run_traced(hass: HomeAssistant, blob: bytes) -> tuple[list[Any], list[dict[str, Any]]]:
    """Run one turn inside a conversation trace; return (deltas, agent-detail payloads)."""
    with trace.async_conversation_trace() as conv_trace:
        deltas = await _run(hass, blob, chunk=9)
    events = conv_trace.as_dict()["events"]
    return deltas, [event["data"] for event in events if event["event_type"] == "agent_detail"]


def _patch_session(mp: pytest.MonkeyPatch, session: MagicMock) -> None:
    def _get(_hass: HomeAssistant) -> MagicMock:
        return session

    mp.setattr("custom_components.llm_middleman.backends.dify.async_get_clientsession", _get)


def _probe_session(info: FakeJsonResponse, parameters: FakeJsonResponse | None = None) -> MagicMock:
    """Session answering ``GET /info`` and ``GET /parameters`` by URL."""
    params = parameters if parameters is not None else FakeJsonResponse({"user_input_form": []})

    def _get(url: str, **_kwargs: Any) -> FakeJsonResponse:
        return params if url == _PARAMS_URL else info

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


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
    blob = _dify_sse(_msg("Hello ", event=event), _msg("world", event=event), _end(), newline=newline)
    deltas = await _run(hass, blob, chunk=5)
    assert deltas[0] == {"role": "assistant"}
    assert text_of(deltas) == "Hello world"


async def test_message_end_terminates_stream(hass: HomeAssistant) -> None:
    # Content after message_end is never emitted (message_end is terminal).
    blob = _dify_sse(_msg("Hi", event="message"), _end(), _msg("after-end", event="message"))
    assert text_of(await _run(hass, blob, chunk=6)) == "Hi"


async def test_legacy_agent_file_and_tts_events_produce_no_content(hass: HomeAssistant) -> None:
    file_event = _ev("message_file", id="file_id_1", type="image", belongs_to="assistant", url="https://x/cat.png")
    blob = _dify_sse(_PING, file_event, _ev("tts_message", audio="..."), _msg("answer"), _end())
    assert await _run(hass, blob, chunk=7) == [{"role": "assistant"}, {"content": "answer"}]


async def test_agent_closing_full_message_not_spoken_twice(hass: HomeAssistant) -> None:
    # New-style Agent app: agent_message chunks, then ONE closing message with the full answer.
    blob = _dify_sse(
        _ev("agent_thought", id="t-1", position=1, thought="", tool="", tool_input="", observation=""),
        _msg("Hi "),
        _msg("there"),
        _msg("Hi there", event="message"),
        _end(usage={"total_tokens": 50, "latency": 2.5}),
    )
    assert text_of(await _run(hass, blob, chunk=9)) == "Hi there"


async def test_message_end_flushes_held_think_fragment(hass: HomeAssistant) -> None:
    # A trailing "<" held back by the think-stripper is flushed by message_end, not by EOF.
    blob = _dify_sse(_msg("a <", event="message"), _end(), _msg("think>x</think>", event="message"))
    assert text_of(await _run(hass, blob, chunk=6)) == "a <"


async def test_sse_event_line_fallback_when_json_has_no_event(hass: HomeAssistant) -> None:
    # A proxy that frames with ``event:`` lines and omits the JSON key still works.
    def bare(answer: str) -> str:
        return json.dumps({"answer": answer, "task_id": _TASK, "conversation_id": _CONV})

    blob = sse_bytes(
        ("agent_message", bare("Hi")),
        ("message_end", json.dumps({"conversation_id": _CONV})),
        ("agent_message", bare("after-end")),
    )
    assert text_of(await _run(hass, blob, chunk=6)) == "Hi"


# --- chatflow outcomes ------------------------------------------------------


async def test_chatflow_success_speaks_answer_and_shows_reasoning(hass: HomeAssistant) -> None:
    # Spec "Response Example - Streaming (Workflow)" incl. separated reasoning_chunk deltas.
    blob = _dify_sse(
        _PING,
        _WF_STARTED,
        _NODE_STARTED,
        _reasoning("The user greeted me, so"),
        _reasoning("", is_final=True),
        _msg("Hello", event="message"),
        _msg(" world", event="message"),
        _wf("node_finished", **_LLM_NODE, status="succeeded", elapsed_time=1.5, outputs={"text": "Hello world"}),
        _end(),
        _wf("workflow_finished", id=_RUN, workflow_id="wf_def456", status="succeeded", outputs={"answer": "x"}),
    )
    assert await _run(hass, blob, chunk=11) == [
        {"role": "assistant"},
        {"thinking_content": "The user greeted me, so"},
        {"content": "Hello"},
        {"content": " world"},
    ]


async def test_chatflow_failure_raises_and_stops_task(hass: HomeAssistant) -> None:
    # Failure: workflow_finished(failed) then error; no message_end.
    failed = _wf("workflow_finished", id=_RUN, workflow_id="wf_def456", status="failed", error="LLM node failed")
    rec = _Recorder(_reply(_dify_sse(_PING, _WF_STARTED, _NODE_STARTED, failed, _error())))
    adapter = _adapter(hass, rec.session())

    with pytest.raises(BackendStreamError):
        await collect(adapter.stream_turn(_chat_log(hass), make_input(), TurnContext(options={}, memory_key="k")))
    await hass.async_block_till_done()

    assert f"{_CHAT_URL}/{_TASK}/stop" in rec.urls


async def test_chatflow_pause_speaks_notice_and_traces_without_stopping(hass: HomeAssistant) -> None:
    # Pause: human_input_required then workflow_paused; the stream ends there.
    human = _wf("human_input_required", form_id="f-1", form_token="tok_abc123", form_content="Please review.")
    paused = _wf("workflow_paused", workflow_run_id=_RUN, paused_nodes=["approval_node"], status="paused")
    rec = _Recorder(_reply(_dify_sse(_PING, _WF_STARTED, human, paused)))
    adapter = _adapter(hass, rec.session())

    with trace.async_conversation_trace() as conv_trace:
        deltas = await collect(
            adapter.stream_turn(_chat_log(hass), make_input(), TurnContext(options={}, memory_key="k"))
        )
    await hass.async_block_till_done()

    assert deltas == [{"role": "assistant"}, {"content": PAUSED_NOTICE}]
    assert rec.urls == [_CHAT_URL]  # the paused run is left for Dify's human-input form
    details = [event["data"] for event in conv_trace.as_dict()["events"]]
    assert {"dify_workflow_paused": paused["data"]} in details


async def test_chatflow_pause_after_text_appends_notice(hass: HomeAssistant) -> None:
    paused = _wf("workflow_paused", workflow_run_id=_RUN, paused_nodes=["approval_node"], status="paused")
    blob = _dify_sse(_WF_STARTED, _msg("Drafted.", event="message"), paused)
    assert text_of(await _run(hass, blob, chunk=9)) == f"Drafted. {PAUSED_NOTICE}"


# --- truncated streams ------------------------------------------------------


async def test_eof_after_text_without_message_end_raises(hass: HomeAssistant) -> None:
    # A dropped connection must not pass a partial reply off as complete.
    with pytest.raises(BackendStreamError):
        await _run(hass, _dify_sse(_msg("partial ", event="message")), chunk=7)


async def test_eof_without_any_event_raises(hass: HomeAssistant) -> None:
    with pytest.raises(BackendStreamError):
        await _run(hass, _dify_sse(_PING), chunk=7)


# --- in-stream errors -------------------------------------------------------


async def test_mid_stream_error_after_partial_text_raises_and_logs(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    # Real wire: the error is a bare data frame. It must not be swallowed as "message".
    blob = _dify_sse(_msg("partial ", event="message"), _error(code="boom", message="bad"))
    adapter = _adapter(hass, _session(blob, chunk=7))
    gen = adapter.stream_turn(_chat_log(hass), make_input(), TurnContext(options={}, memory_key="k"))
    assert await anext(gen) == {"role": "assistant"}
    assert await anext(gen) == {"content": "partial "}
    with pytest.raises(BackendStreamError):
        await anext(gen)
    assert "code=boom message=bad" in caplog.text


# --- message_replace (output moderation) ------------------------------------


async def test_message_replace_before_text_speaks_replacement(hass: HomeAssistant) -> None:
    blob = _dify_sse(
        _ev("message_replace", answer="I can't help with that.", reason="moderation"),
        _msg("original", event="message"),
        _end(),
    )
    deltas, details = await _run_traced(hass, blob)
    assert deltas == [{"role": "assistant"}, {"content": "I can't help with that."}]
    assert {"dify_message_replace": {"answer": "I can't help with that.", "reason": "moderation"}} in details


async def test_message_replace_after_text_is_traced_not_spoken(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    blob = _dify_sse(
        _msg("bad words", event="message"),
        _ev("message_replace", answer="[redacted]", reason="moderation"),
        _msg(" more", event="message"),
        _end(),
    )
    deltas, details = await _run_traced(hass, blob)
    assert text_of(deltas) == "bad words"
    assert {"dify_message_replace": {"answer": "[redacted]", "reason": "moderation"}} in details
    assert "already spoken" in caplog.text


# --- usage ------------------------------------------------------------------


async def test_usage_recorded_in_trace(hass: HomeAssistant) -> None:
    _, details = await _run_traced(hass, _dify_sse(_msg("ok", event="message"), _end()))
    assert details[-2:] == [{"stats": {"input_tokens": 1033, "output_tokens": 128}}, {"dify_usage": _USAGE}]


async def test_usage_without_token_split_omits_stats(hass: HomeAssistant) -> None:
    # The spec's Agent example reports only total_tokens/latency.
    usage = {"total_tokens": 50, "latency": 2.5}
    _, details = await _run_traced(hass, _dify_sse(_msg("ok"), _end(usage=usage)))
    assert details == [{"dify_usage": usage}]


# --- <think> reasoning stripped from spoken output --------------------------
# Reasoning models (e.g. via LiteLLM/llama-swap) inline chain-of-thought as
# <think>…</think> in the answer; agent-chat apps have no post-processing node,
# so the raw reasoning reaches the answer stream. HA speaks every content delta
# aloud, so the adapter must strip think blocks before yielding — keeping only
# the final prose. Blocks can span multiple deltas and the tags can split across
# chunk boundaries.


async def test_think_blocks_stripped_from_answer(hass: HomeAssistant) -> None:
    blob = _dify_sse(
        _msg("<think>\nLet me check the time.\n</think>"),
        _msg("<think>\nGot it.\n</think>"),
        _msg("It's twelve thirty-four PM, sir."),
        _end(),
    )
    deltas = await _run(hass, blob, chunk=3)
    assert text_of(deltas) == "It's twelve thirty-four PM, sir."
    # role-first invariant: emitted exactly once, and only once real content exists.
    assert deltas[0] == {"role": "assistant"}
    assert sum(1 for d in deltas if d.get("role")) == 1


async def test_think_tags_split_across_deltas_are_stripped(hass: HomeAssistant) -> None:
    blob = _dify_sse(_msg("<th"), _msg("ink>reasoning"), _msg(" here</th"), _msg("ink>real answer"), _end())
    assert text_of(await _run(hass, blob, chunk=4)) == "real answer"


async def test_unclosed_think_emits_no_content(hass: HomeAssistant) -> None:
    # An open <think> with no close must never speak the partial reasoning.
    blob = _dify_sse(_msg("<think>\nthinking but cut off"), _end())
    assert text_of(await _run(hass, blob, chunk=4)) == ""


async def test_literal_angle_bracket_text_preserved(hass: HomeAssistant) -> None:
    blob = _dify_sse(_msg("price is < 5 dollars"), _end())
    assert text_of(await _run(hass, blob, chunk=3)) == "price is < 5 dollars"


# --- request shape ----------------------------------------------------------


async def test_request_shape_first_turn_omits_conversation_id(hass: HomeAssistant) -> None:
    rec = _Recorder(_reply(_dify_sse(_msg("ok"), _end())))
    session = rec.session()
    adapter = _adapter(hass, session)
    await collect(adapter.stream_turn(_chat_log(hass), make_input("hello"), TurnContext(options={}, memory_key="k")))

    assert rec.urls == [_CHAT_URL]
    assert rec.bodies[0] == {
        "query": "hello",
        "inputs": {},
        "response_mode": "streaming",
        "user": "home-assistant",
        "auto_generate_name": False,
    }
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer app-key"


# --- end-user identity ------------------------------------------------------


async def test_user_option_sends_ha_user_id(hass: HomeAssistant) -> None:
    rec = _Recorder(_reply(_dify_sse(_msg("ok"), _end())))
    adapter = _adapter(hass, rec.session())
    ctx = TurnContext(options={"dify_user_from_ha": True}, memory_key="k")
    await collect(adapter.stream_turn(_chat_log(hass), make_input(user_id="ha-user-1"), ctx))
    assert rec.bodies[0]["user"] == "ha-user-1"


async def test_user_option_falls_back_without_ha_user(hass: HomeAssistant) -> None:
    # Voice-satellite turns carry no HA user: keep the shared identity.
    rec = _Recorder(_reply(_dify_sse(_msg("ok"), _end())))
    adapter = _adapter(hass, rec.session())
    ctx = TurnContext(options={"dify_user_from_ha": True}, memory_key="k")
    await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))
    assert rec.bodies[0]["user"] == "home-assistant"


# --- inputs -----------------------------------------------------------------


def _form(*controls: tuple[str, dict[str, Any]]) -> FakeJsonResponse:
    """A ``GET /parameters`` body whose user_input_form declares ``controls``."""
    return FakeJsonResponse({"opening_statement": "", "user_input_form": [{kind: c} for kind, c in controls]})


def _field(variable: str, *, required: bool = False, **extra: Any) -> dict[str, Any]:
    return {"variable": variable, "label": variable, "required": required, "default": "", **extra}


async def _turn_with_inputs(
    hass: HomeAssistant,
    options: dict[str, Any],
    *,
    parameters: FakeJsonResponse | None = None,
    chat_log: conversation.ChatLog | None = None,
    user_input: conversation.ConversationInput | None = None,
) -> tuple[dict[str, Any], MagicMock]:
    """Run a turn with ``options``; return (the posted ``inputs``, the session)."""
    rec = _Recorder(_reply(_dify_sse(_msg("ok"), _end())))
    session = rec.session()
    session.get = MagicMock(return_value=parameters or _form())
    adapter = _adapter(hass, session)
    ctx = TurnContext(options=options, memory_key="k")
    await collect(adapter.stream_turn(chat_log or _chat_log(hass), user_input or make_input(), ctx))
    return rec.bodies[0]["inputs"], session


async def test_static_inputs_mapping_is_sent(hass: HomeAssistant) -> None:
    inputs, session = await _turn_with_inputs(hass, {"dify_inputs": {"city": "Berlin", "units": 2}})
    assert inputs == {"city": "Berlin", "units": 2}
    session.get.assert_not_called()  # no /parameters fetch without the HA-context option


async def test_ha_context_fills_only_declared_variables(hass: HomeAssistant) -> None:
    # A satellite start_conversation prompt lives on the chat log for the follow-up turn.
    parameters = _form(
        ("text-input", _field("ha_language")),
        ("paragraph", _field("ha_extra_system_prompt")),
        ("text-input", _field("city", required=True)),
    )
    chat_log = _chat_log(hass, extra_system_prompt="You asked: close the garage door?")
    inputs, _ = await _turn_with_inputs(
        hass, {"dify_ha_context": True, "dify_inputs": {"city": "Berlin"}}, parameters=parameters, chat_log=chat_log
    )
    assert inputs == {
        "ha_language": "en",
        "ha_extra_system_prompt": "You asked: close the garage door?",
        "city": "Berlin",
    }


async def test_ha_context_resolves_satellite_area(hass: HomeAssistant) -> None:
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Kitchen")
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(config_entry_id=owner.entry_id, identifiers={("test", "sat-1")})
    devices.async_update_device(device.id, area_id=area.id)

    parameters = _form(("text-input", _field("ha_area")), ("text-input", _field("ha_device_id")))
    inputs, _ = await _turn_with_inputs(
        hass, {"dify_ha_context": True}, parameters=parameters, user_input=make_input(device_id=device.id)
    )
    assert inputs == {"ha_area": "Kitchen", "ha_device_id": device.id}


async def test_ha_context_truncated_to_declared_max_length(hass: HomeAssistant) -> None:
    parameters = _form(("text-input", _field("ha_extra_system_prompt", max_length=10)))
    chat_log = _chat_log(hass, extra_system_prompt="0123456789-overflow")
    inputs, _ = await _turn_with_inputs(hass, {"dify_ha_context": True}, parameters=parameters, chat_log=chat_log)
    assert inputs == {"ha_extra_system_prompt": "0123456789"}


async def test_declared_inputs_cached_across_turns(hass: HomeAssistant) -> None:
    rec = _Recorder(_reply(_dify_sse(_msg("ok"), _end())), _reply(_dify_sse(_msg("ok"), _end())))
    session = rec.session()
    session.get = MagicMock(return_value=_form(("text-input", _field("ha_language"))))
    adapter = _adapter(hass, session)
    ctx = TurnContext(options={"dify_ha_context": True}, memory_key="k")

    await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))
    await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))

    assert session.get.call_count == 1
    assert session.get.call_args.args[0] == _PARAMS_URL


async def test_declared_inputs_refetched_after_ttl(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.llm_middleman.backends.dify._PARAMETERS_TTL", 0.0)
    rec = _Recorder(_reply(_dify_sse(_msg("ok"), _end())), _reply(_dify_sse(_msg("ok"), _end())))
    session = rec.session()
    session.get = MagicMock(return_value=_form(("text-input", _field("ha_language"))))
    adapter = _adapter(hass, session)
    ctx = TurnContext(options={"dify_ha_context": True}, memory_key="k")

    await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))
    await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))

    assert session.get.call_count == 2


async def test_parameters_fetch_failure_does_not_fail_turn(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    inputs, _ = await _turn_with_inputs(hass, {"dify_ha_context": True}, parameters=FakeJsonResponse(status=500))
    assert inputs == {}
    assert "sending no HA context inputs" in caplog.text


async def test_invalid_param_400_logs_inputs_hint(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    body = '{"code": "invalid_param", "message": "city is required in input form", "status": 400}'
    with pytest.raises(BackendConnectionError):
        await _run(hass, b"", status=400, text=body)
    assert "Dify inputs option" in caplog.text


# --- conversation-id capture / echo / persistence ---------------------------


async def test_second_turn_echoes_captured_conversation_id(hass: HomeAssistant) -> None:
    blob = _dify_sse(_msg("ok", cid="conv-xyz"), _end(cid="conv-xyz"))
    rec = _Recorder(_reply(blob), _reply(blob))
    adapter = _adapter(hass, rec.session())
    ctx = TurnContext(options={}, memory_key="k")
    await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))
    await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))

    assert "conversation_id" not in rec.bodies[0]  # first turn: no id
    assert rec.bodies[1]["conversation_id"] == "conv-xyz"  # second turn: echoes captured id


async def test_conversation_id_captured_from_workflow_started(hass: HomeAssistant) -> None:
    # Chatflow: the id is known from the first event, before any answer (here: none at all).
    rec = _Recorder(_reply(_dify_sse(_WF_STARTED, _error())), _reply(_dify_sse(_msg("ok"), _end())))
    adapter = _adapter(hass, rec.session())
    ctx = TurnContext(options={}, memory_key="k")
    with pytest.raises(BackendStreamError):
        await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))
    await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))

    assert rec.bodies[-1]["conversation_id"] == _CONV


async def test_conversation_scope_not_persisted(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    await _run(hass, _dify_sse(_msg("ok", cid="conv-c"), _end(cid="conv-c")), chunk=8)
    assert "llm_middleman.dify.e1" not in hass_storage


async def test_device_map_persists_across_restart(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    blob = _dify_sse(_msg("ok", cid="conv-dev"), _end(cid="conv-dev"))
    device_ctx = TurnContext(options={"memory_scope": "device"}, memory_key="dev-A")

    adapter1 = _adapter(hass, _session(blob, chunk=8))
    await collect(adapter1.stream_turn(_chat_log(hass), make_input(), device_ctx))
    assert hass_storage["llm_middleman.dify.e1"]["data"] == {"dev-A": "conv-dev"}

    # "Restart": a fresh adapter (same entry) reloads the map and echoes the stored id.
    rec = _Recorder(_reply(blob))
    adapter2 = _adapter(hass, rec.session())
    await collect(adapter2.stream_turn(_chat_log(hass), make_input(), device_ctx))
    assert rec.bodies[0]["conversation_id"] == "conv-dev"


async def test_legacy_device_conversation_adopted_once_by_first_agent(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    # Device keys became per agent; the first agent on the device keeps the old shared
    # conversation, a sibling agent starts its own instead of sharing it.
    hass_storage["llm_middleman.dify.e1"] = {"version": 1, "data": {"dev-A": "conv-old"}}
    blob = _dify_sse(_msg("ok", cid="conv-new"), _end(cid="conv-new"))
    rec = _Recorder(_reply(_dify_sse(_msg("ok", cid="conv-old"), _end(cid="conv-old"))), _reply(blob))
    adapter = _adapter(hass, rec.session())
    first = TurnContext(options={"memory_scope": "device"}, memory_key="sub-1:dev-A", legacy_memory_key="dev-A")
    second = TurnContext(options={"memory_scope": "device"}, memory_key="sub-2:dev-A", legacy_memory_key="dev-A")
    await collect(adapter.stream_turn(_chat_log(hass), make_input(), first))
    await collect(adapter.stream_turn(_chat_log(hass), make_input(), second))

    assert rec.bodies[0]["conversation_id"] == "conv-old"
    assert "conversation_id" not in rec.bodies[1]
    assert hass_storage["llm_middleman.dify.e1"]["data"] == {"sub-1:dev-A": "conv-old", "sub-2:dev-A": "conv-new"}


# Dify has shipped >1 wire shape for a stale/deleted conversation_id 404 (the spec's
# ``not_found`` + "Conversation Not Exists." and an older ``conversation_not_exists``), and
# an ended conversation answers 400 ``conversation_completed``. Each must drop-and-recreate,
# or the stale id wedges the agent forever.
@pytest.mark.parametrize(
    ("status", "gone_body"),
    [
        (404, '{"code":"not_found","message":"Conversation Not Exists.","status":404}'),
        (404, '{"code":"conversation_not_exists","message":"gone"}'),
        (
            400,
            '{"code":"conversation_completed","message":"The conversation has ended. '
            'Please start a new conversation.","status":400}',
        ),
    ],
)
async def test_gone_conversation_dropped_and_retried_once(hass: HomeAssistant, status: int, gone_body: str) -> None:
    rec = _Recorder(
        _reply(_dify_sse(_msg("first", cid="old-id"), _end(cid="old-id"))),
        FakeStreamResponse([], status=status, text=gone_body),
        _reply(_dify_sse(_msg("recovered", cid="new-id"), _end(cid="new-id"))),
    )
    adapter = _adapter(hass, rec.session())
    ctx = TurnContext(options={}, memory_key="k")

    await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))  # turn 1 -> captures old-id
    deltas = await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))  # turn 2 -> gone -> retry

    assert rec.bodies[1]["conversation_id"] == "old-id"  # stale id echoed
    assert "conversation_id" not in rec.bodies[2]  # retried once without id
    assert text_of(deltas) == "recovered"


# --- pre-stream errors ------------------------------------------------------


async def test_pre_stream_auth_error_raises(hass: HomeAssistant) -> None:
    with pytest.raises(BackendAuthError):
        await _run(hass, b"", status=401, chunk=1)


async def test_pre_stream_non_2xx_raises_connection_error(hass: HomeAssistant) -> None:
    with pytest.raises(BackendConnectionError):
        await _run(hass, b"", status=500, chunk=1)


async def test_unrelated_404_raises_connection_error(hass: HomeAssistant) -> None:
    with pytest.raises(BackendConnectionError):
        await _run(hass, b"", status=404, text='{"code":"not_found","message":"Workflow not found"}')


@pytest.mark.parametrize("code", ["too_many_requests", "rate_limit_error"])
async def test_rate_limit_429_logs_specific_reason(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, code: str
) -> None:
    body = json.dumps({"code": code, "message": "Too many requests.", "status": 429})
    with pytest.raises(BackendConnectionError):
        await _run(hass, b"", status=429, text=body)
    assert f"HTTP 429 ({code})" in caplog.text


# --- best-effort stop -------------------------------------------------------


async def test_stop_fired_on_cancel_midstream_with_turn_user(hass: HomeAssistant) -> None:
    # No message_end: the generator stays suspended at the content yield.
    rec = _Recorder(_reply(_dify_sse(_msg("Hi", task_id="task-9"))))
    adapter = _adapter(hass, rec.session())
    ctx = TurnContext(options={"dify_user_from_ha": True}, memory_key="k")
    gen = adapter.stream_turn(_chat_log(hass), make_input(user_id="ha-user-1"), ctx)

    assert await anext(gen) == {"role": "assistant"}
    assert await anext(gen) == {"content": "Hi"}
    await gen.aclose()  # GeneratorExit mid-stream, task_id already captured
    await hass.async_block_till_done()

    assert f"{_CHAT_URL}/task-9/stop" in rec.urls
    assert rec.stop_bodies() == [{"user": "ha-user-1"}]  # stop is a no-op under another user


async def test_stop_fired_on_cancel_during_chatflow_pre_answer_phase(hass: HomeAssistant) -> None:
    # Only workflow_started has arrived (no answer yet) when HA cancels the turn.
    response = FakeStreamResponse([])
    content = HangingStreamContent(_dify_sse(_PING, _WF_STARTED, _NODE_STARTED))
    response.content = content  # type: ignore[assignment]
    rec = _Recorder(response)
    adapter = _adapter(hass, rec.session())
    task = asyncio.create_task(
        collect(adapter.stream_turn(_chat_log(hass), make_input(), TurnContext(options={}, memory_key="k")))
    )
    await content.reached.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await hass.async_block_till_done()

    assert f"{_CHAT_URL}/{_TASK}/stop" in rec.urls


async def test_stop_fired_on_idle_timeout(hass: HomeAssistant) -> None:
    # sock_read idle timeout mid-chatflow: the Dify task must not keep running.
    blob = _dify_sse(_WF_STARTED, _msg("partial", event="message"))
    rec = _Recorder(FakeStreamResponse([blob], raise_after=1, exc=TimeoutError("idle")))
    adapter = _adapter(hass, rec.session())

    with pytest.raises(TimeoutError):
        await collect(adapter.stream_turn(_chat_log(hass), make_input(), TurnContext(options={}, memory_key="k")))
    await hass.async_block_till_done()

    assert rec.stop_bodies() == [{"user": "home-assistant"}]


async def test_stop_failure_is_swallowed(hass: HomeAssistant) -> None:
    def handler(url: str, **_kwargs: Any) -> FakeStreamResponse:
        if url.endswith("/stop"):
            raise aiohttp.ClientConnectionError("gone")
        return FakeStreamResponse(chunk_bytes(_dify_sse(_msg("Hi")), 8))

    session = MagicMock()
    session.post = MagicMock(side_effect=handler)
    adapter = _adapter(hass, session)
    gen = adapter.stream_turn(_chat_log(hass), make_input(), TurnContext(options={}, memory_key="k"))
    await anext(gen)
    await anext(gen)
    await gen.aclose()
    await hass.async_block_till_done()  # the failed stop must not surface


async def test_no_stop_on_cancel_before_task_id_seen(hass: HomeAssistant) -> None:
    # A content event with no task_id, and no message_end: the generator suspends at
    # the content yield with no task_id ever captured.
    rec = _Recorder(_reply(_dify_sse({"event": "agent_message", "answer": "Hi", "message_id": _MSG})))
    adapter = _adapter(hass, rec.session())
    gen = adapter.stream_turn(_chat_log(hass), make_input(), TurnContext(options={}, memory_key="k"))

    assert await anext(gen) == {"role": "assistant"}
    assert await anext(gen) == {"content": "Hi"}
    await gen.aclose()
    await hass.async_block_till_done()

    assert rec.urls == [_CHAT_URL]  # the turn ran; no /stop fired


# --- connection validation --------------------------------------------------


@pytest.mark.parametrize("mode", ["chat", "agent-chat", "advanced-chat", "agent"])
async def test_validate_accepts_chat_modes(hass: HomeAssistant, mode: str) -> None:
    session = _probe_session(FakeJsonResponse({"mode": mode, "name": "app"}))
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        await DifyAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, "api_key": "app-key"})
    probed = [call.args[0] for call in session.get.call_args_list]
    assert probed == [f"{_BASE_URL}/info", _PARAMS_URL]
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer app-key"


@pytest.mark.parametrize("mode", ["workflow", "completion"])
async def test_validate_rejects_non_chat_mode(hass: HomeAssistant, mode: str) -> None:
    session = _probe_session(FakeJsonResponse({"mode": mode}))
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        with pytest.raises(DifyAppError) as err:
            await DifyAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, "api_key": "k"})
    assert err.value.error_key == "not_chat_app"


async def test_validate_rejects_unpublished_agent(hass: HomeAssistant) -> None:
    unpublished = FakeJsonResponse(
        {"code": "agent_not_published", "message": "Agent has not been published."}, status=400
    )
    session = _probe_session(FakeJsonResponse({"mode": "agent"}), unpublished)
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        with pytest.raises(DifyAppError) as err:
            await DifyAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, "api_key": "k"})
    assert err.value.error_key == "agent_not_published"


async def test_validate_parameters_error_status(hass: HomeAssistant) -> None:
    session = _probe_session(
        FakeJsonResponse({"mode": "chat"}), FakeJsonResponse({"code": "app_unavailable"}, status=400)
    )
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        with pytest.raises(BackendStatusError):
            await DifyAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, "api_key": "k"})


async def test_validate_auth_error(hass: HomeAssistant) -> None:
    session = _probe_session(FakeJsonResponse(status=401))
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


async def test_fetch_input_form_parses_declared_variables(hass: HomeAssistant) -> None:
    city = _field("city", required=True)
    mood = {"variable": "mood", "label": "Mood", "required": False, "default": "calm", "options": ["calm"]}
    session = _probe_session(FakeJsonResponse(), _form(("text-input", city), ("select", mood)))
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        declared = await DifyAdapter.async_fetch_input_form(hass, {"base_url": _BASE_URL, "api_key": "k"})
    assert declared == {"city": city, "mood": mood}


async def test_fetch_input_form_rejects_non_object_body(hass: HomeAssistant) -> None:
    session = _probe_session(FakeJsonResponse(), FakeJsonResponse(raw="<html>proxy error</html>"))
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        with pytest.raises(BackendConnectionError):
            await DifyAdapter.async_fetch_input_form(hass, {"base_url": _BASE_URL, "api_key": "k"})


async def test_fetch_input_form_skips_malformed_entries(hass: HomeAssistant) -> None:
    city = _field("city")
    body: dict[str, Any] = {
        "user_input_form": [{"text-input": city}, "junk", {"a": {}, "b": {}}, {"paragraph": "x"}, {"select": {}}]
    }
    session = _probe_session(FakeJsonResponse(), FakeJsonResponse(body))
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        declared = await DifyAdapter.async_fetch_input_form(hass, {"base_url": _BASE_URL, "api_key": "k"})
    assert declared == {"city": city}


async def test_validate_parameters_auth_error(hass: HomeAssistant) -> None:
    session = _probe_session(FakeJsonResponse({"mode": "chat"}), FakeJsonResponse(status=403))
    with pytest.MonkeyPatch.context() as mp:
        _patch_session(mp, session)
        with pytest.raises(BackendAuthError):
            await DifyAdapter.async_validate_connection(hass, {"base_url": _BASE_URL, "api_key": "k"})


async def test_gone_conversation_dropped_from_persisted_map(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    gone = '{"code":"not_found","message":"Conversation Not Exists.","status":404}'
    rec = _Recorder(
        _reply(_dify_sse(_msg("first", cid="old-id"), _end(cid="old-id"))),
        FakeStreamResponse([], status=404, text=gone),
        _reply(_dify_sse(_msg("recovered", cid="new-id"), _end(cid="new-id"))),
    )
    adapter = _adapter(hass, rec.session())
    ctx = TurnContext(options={"memory_scope": "device"}, memory_key="dev-A")

    await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))
    await collect(adapter.stream_turn(_chat_log(hass), make_input(), ctx))

    assert "conversation_id" not in rec.bodies[2]
    assert hass_storage["llm_middleman.dify.e1"]["data"] == {"dev-A": "new-id"}


# --- tool activity (external tool calls in the Assist chat) -------------------------


_THOUGHT_ID = "8dcf3648-fbad-407a-85dd-73a6f43aeb9f"


def _thought(*, tool: str, tool_input: str = "", observation: str = "", thought: str = "") -> dict[str, Any]:
    # Spec StreamEventChatAgentThought; Dify re-sends the same thought id as it fills in.
    return _ev(
        "agent_thought",
        id=_THOUGHT_ID,
        position=1,
        thought=thought,
        tool=tool,
        tool_input=tool_input,
        observation=observation,
        message_files=[],
    )


def _tool_node(event: str, **fields: Any) -> dict[str, Any]:
    node = {"id": "ne_tool_1", "node_id": "node_weather", "node_type": "tool", "title": "Weather", "index": 2}
    return _wf(event, **node, **fields)


def _tool_events(deltas: list[Any]) -> list[tuple[Any, ...]]:
    """Flatten external tool calls/results out of a delta stream."""
    out: list[tuple[Any, ...]] = []
    for delta in deltas:
        raw = cast("dict[str, Any]", delta)
        calls = cast("list[llm.ToolInput]", raw.get("tool_calls") or [])
        out.extend(("call", c.id, c.tool_name, c.tool_args, c.external) for c in calls)
        if raw.get("role") == "tool_result":
            data = raw["result"].data if "result" in raw else raw["tool_result"]
            out.append(("result", raw["tool_call_id"], raw["tool_name"], data))
    return out


async def test_agent_thought_tool_call_and_result_emitted_once(hass: HomeAssistant) -> None:
    # Spec Agent example: the call appears with tool_input, the result once observation fills in.
    blob = _dify_sse(
        _thought(tool="dalle3", tool_input='{"dalle3": {"prompt": "a cute cat"}}', thought="Calling a tool..."),
        _thought(tool="dalle3", tool_input='{"dalle3": {"prompt": "a cute cat"}}', observation="image created"),
        _thought(tool="dalle3", tool_input='{"dalle3": {"prompt": "a cute cat"}}', observation="image created"),
        _msg("Here is the image."),
        _end(),
    )
    deltas = await _run(hass, blob, chunk=13)
    call_id = f"{_THOUGHT_ID}:dalle3"
    assert _tool_events(deltas) == [
        ("call", call_id, "dalle3", {"prompt": "a cute cat"}, True),
        ("result", call_id, "dalle3", {"result": "image created"}),
    ]
    # The answer opens a fresh assistant block after the tool result; tool I/O is never spoken.
    assert deltas[-2:] == [{"role": "assistant"}, {"content": "Here is the image."}]
    assert text_of(deltas) == "Here is the image."


async def test_agent_thought_with_several_tools_splits_inputs_and_observations(hass: HomeAssistant) -> None:
    blob = _dify_sse(
        _thought(
            tool="weather;clock",
            tool_input='{"weather": {"city": "Berlin"}, "clock": {"tz": "CET"}}',
            observation='{"weather": "sunny", "clock": {"time": "12:00"}}',
        ),
        _msg("Sunny, noon."),
        _end(),
    )
    events = _tool_events(await _run(hass, blob, chunk=17))
    assert events == [
        ("call", f"{_THOUGHT_ID}:weather", "weather", {"city": "Berlin"}, True),
        ("result", f"{_THOUGHT_ID}:weather", "weather", {"result": "sunny"}),
        ("call", f"{_THOUGHT_ID}:clock", "clock", {"tz": "CET"}, True),
        ("result", f"{_THOUGHT_ID}:clock", "clock", {"time": "12:00"}),
    ]


@pytest.mark.parametrize(
    ("tool_input", "args"),
    [('{"query": "lights"}', {"query": "lights"}), ("turn on the lights", {"input": "turn on the lights"})],
)
async def test_agent_thought_unkeyed_or_plain_tool_input(
    hass: HomeAssistant, tool_input: str, args: dict[str, Any]
) -> None:
    blob = _dify_sse(_thought(tool="search", tool_input=tool_input), _msg("Done."), _end())
    assert _tool_events(await _run(hass, blob, chunk=11)) == [("call", f"{_THOUGHT_ID}:search", "search", args, True)]


async def test_agent_thought_without_tool_emits_nothing(hass: HomeAssistant) -> None:
    blob = _dify_sse(_thought(tool="", thought="Let me think."), _msg("Hi"), _end())
    assert await _run(hass, blob, chunk=9) == [{"role": "assistant"}, {"content": "Hi"}]


async def test_chatflow_tool_node_emits_call_and_result(hass: HomeAssistant) -> None:
    blob = _dify_sse(
        _WF_STARTED,
        _tool_node("node_started", inputs={"city": "Berlin"}),
        _tool_node("node_finished", inputs={"city": "Berlin"}, status="succeeded", outputs={"text": "sunny"}),
        _NODE_STARTED,  # an llm node: not a tool
        _msg("It is sunny.", event="message"),
        _end(),
    )
    deltas = await _run(hass, blob, chunk=11)
    assert _tool_events(deltas) == [
        ("call", "ne_tool_1", "Weather", {"city": "Berlin"}, True),
        ("result", "ne_tool_1", "Weather", {"text": "sunny"}),
    ]
    assert text_of(deltas) == "It is sunny."


async def test_chatflow_failed_tool_node_records_error_result(hass: HomeAssistant) -> None:
    failed = _tool_node("node_finished", inputs={}, status="failed", error="HTTP 500 from weather API")
    blob = _dify_sse(_WF_STARTED, failed, _msg("Weather is unavailable.", event="message"), _end())
    events = _tool_events(await _run(hass, blob, chunk=11))
    # node_finished without a seen node_started still emits the call before its result.
    assert events == [
        ("call", "ne_tool_1", "Weather", {}, True),
        ("result", "ne_tool_1", "Weather", {"error": "HTTP 500 from weather API"}),
    ]


async def test_tools_without_answer_raise_without_stopping(hass: HomeAssistant) -> None:
    blob = _dify_sse(_thought(tool="search", tool_input='{"q": "x"}', observation="found"), _end())
    rec = _Recorder(_reply(blob))
    adapter = _adapter(hass, rec.session())
    with pytest.raises(BackendStreamError):
        await collect(adapter.stream_turn(_chat_log(hass), make_input(), TurnContext(options={}, memory_key="k")))
    await hass.async_block_till_done()
    assert rec.urls == [_CHAT_URL]  # message_end arrived: the task is over, nothing to stop


# --- through the entity (tool loop) -----------------------------------------------


_TOOL_TURN = _dify_sse(
    _thought(tool="lights", tool_input='{"lights": {"area": "kitchen"}}'),
    _thought(tool="lights", tool_input='{"lights": {"area": "kitchen"}}', observation="ok"),
    _msg("Kitchen lights are on."),
    _end(),
)


async def test_turn_with_tool_activity_posts_exactly_once(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """External tool results must not re-enter the entity's HA tool loop (one POST per turn)."""
    rec = _Recorder(_reply(_TOOL_TURN), _reply(_TOOL_TURN))
    entity = build_entity(hass, _adapter(hass, rec.session()), {})

    result = await entity._async_handle_message(make_input(), mock_chat_log)

    assert rec.urls == [_CHAT_URL]
    assert result.response.speech["plain"]["speech"] == "Kitchen lights are on."
    call, tool_result, reply = mock_chat_log.content[-3:]
    assert isinstance(call, conversation.AssistantContent)
    assert call.tool_calls is not None
    assert call.tool_calls[0].external is True
    assert isinstance(tool_result, conversation.ToolResultContent)
    assert isinstance(reply, conversation.AssistantContent)
    assert reply.content == "Kitchen lights are on."


async def test_tools_without_answer_post_once_and_speak_fallback(
    hass: HomeAssistant, mock_chat_log: MockChatLog
) -> None:
    blob = _dify_sse(_thought(tool="lights", tool_input="{}", observation="ok"), _end())
    rec = _Recorder(_reply(blob), _reply(blob))
    entity = build_entity(hass, _adapter(hass, rec.session()), {})

    result = await entity._async_handle_message(make_input(), mock_chat_log)

    assert rec.urls == [_CHAT_URL]
    assert result.response.speech["plain"]["speech"] == ERROR_MESSAGE
