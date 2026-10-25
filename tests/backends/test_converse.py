"""Tests for the custom ``/v1/converse`` adapter (LLMM-009).

Drives **raw bytes** through the real ``_sse`` reader and the adapter — never v0's
pre-split ``_FakeContent`` lines. Exercises the surfaces v0 left untested: multi-line
``data:`` framing, mid-frame chunk splits, ``done`` without a delta, ``error`` after
deltas, silent EOF, malformed JSON, and oversized lines. LLMM-025 adds replays of
stub-derived fixtures (``tests/backends/fixtures/converse``, recorded from
``scripts/e2e/converse_sse_stub.py``), terminal-state discipline, ``tool_activity`` and
the probe's auth check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components import conversation
from homeassistant.components.conversation import trace
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from custom_components.llm_middleman.backends._sse import BackendStreamError
from custom_components.llm_middleman.backends.base import BackendAuthError, TurnContext
from custom_components.llm_middleman.backends.converse import ConverseAdapter
from custom_components.llm_middleman.const import (
    CONF_BASE_URL,
    CONF_TIMEOUT,
    CONF_TOKEN,
    CONVERSE_PATH,
    ERROR_MESSAGE,
)
from tests.conftest import (
    TEST_BASE_URL,
    TEST_TOKEN,
    FakeStreamResponse,
    MockChatLog,
    build_entity,
    chunk_bytes,
    fake_aiohttp_session,
    make_input,
    sse_bytes,
)

Delta = conversation.AssistantContentDeltaDict


def _adapter(
    hass: HomeAssistant,
    response: FakeStreamResponse,
    *,
    token: str | None = None,
) -> ConverseAdapter:
    data: dict[str, str] = {CONF_BASE_URL: TEST_BASE_URL}
    if token is not None:
        data[CONF_TOKEN] = token
    session = fake_aiohttp_session(response=response)
    return ConverseAdapter(hass, session, data)


async def _collect(adapter: ConverseAdapter, ctx: TurnContext, **kw: object) -> list[Delta]:
    device_id = kw.get("device_id")
    assert device_id is None or isinstance(device_id, str)
    chat_log = conversation.ChatLog(adapter.hass, "conv-1")
    return [delta async for delta in adapter.stream_turn(chat_log, make_input("hello", device_id=device_id), ctx)]


async def test_happy_path_streams_and_sets_continue(hass: HomeAssistant) -> None:
    """text_delta then done, split mid-frame; deltas + continue_conversation flow."""
    blob = sse_bytes(
        ("text_delta", '{"delta":"Hi"}'),
        ("done", '{"text":"Hi","continue_conversation":true}'),
        newline=b"\r\n",
    )
    # Split at 5-byte chunks so frame/CRLF boundaries land mid-line.
    response = FakeStreamResponse(chunk_bytes(blob, 5))
    adapter = _adapter(hass, response, token=TEST_TOKEN)
    ctx = TurnContext(options={}, memory_key="mkey")

    deltas = await _collect(adapter, ctx)

    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]
    assert ctx.continue_conversation is True


async def test_request_shape_uses_memory_key_and_bearer(hass: HomeAssistant) -> None:
    """conversation_id == ctx.memory_key; bearer token + URL are correct."""
    response = FakeStreamResponse(chunk_bytes(sse_bytes(("done", '{"text":"ok"}')), 8))
    adapter = _adapter(hass, response, token=TEST_TOKEN)
    ctx = TurnContext(options={}, memory_key="session-key-99")

    await _collect(adapter, ctx, device_id="dev-1")

    call = cast("MagicMock", adapter.session).post.call_args
    assert call.args[0] == TEST_BASE_URL + CONVERSE_PATH
    body = cast("dict[str, Any]", call.kwargs["json"])
    assert body == {
        "conversation_id": "session-key-99",
        "text": "hello",
        "language": "en",
        "device_id": "dev-1",
    }
    headers = cast("dict[str, str]", call.kwargs["headers"])
    assert headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert headers["Accept"] == "text/event-stream"


async def test_no_token_omits_authorization(hass: HomeAssistant) -> None:
    """No token in connection data → no Authorization header, no device_id when absent."""
    response = FakeStreamResponse(chunk_bytes(sse_bytes(("done", '{"text":"ok"}')), 8))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    await _collect(adapter, ctx)

    call = cast("MagicMock", adapter.session).post.call_args
    headers = cast("dict[str, str]", call.kwargs["headers"])
    body = cast("dict[str, Any]", call.kwargs["json"])
    assert "Authorization" not in headers
    assert "device_id" not in body


async def test_multi_line_data_concatenated(hass: HomeAssistant) -> None:
    """Two consecutive data: lines in one event are joined before JSON parsing."""
    blob = sse_bytes(("text_delta", '{"delta":\n"Hi"}'), ("done", "{}"))
    response = FakeStreamResponse(chunk_bytes(blob, 3))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    deltas = await _collect(adapter, ctx)

    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]


async def test_done_without_delta_emits_text(hass: HomeAssistant) -> None:
    """done with no prior delta voices done.text."""
    response = FakeStreamResponse(chunk_bytes(sse_bytes(("done", '{"text":"All set"}')), 4))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    deltas = await _collect(adapter, ctx)

    assert deltas == [{"role": "assistant"}, {"content": "All set"}]
    assert ctx.continue_conversation is False


async def test_done_without_delta_or_text_yields_nothing(hass: HomeAssistant) -> None:
    """done with neither delta nor text says nothing, so the entity guard owns the fallback (LLMM-025)."""
    response = FakeStreamResponse(chunk_bytes(sse_bytes(("done", "{}")), 4))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    deltas = await _collect(adapter, ctx)

    assert deltas == []


async def test_done_after_deltas_discards_done_text(hass: HomeAssistant) -> None:
    """After streamed deltas, done.text is discarded (deltas are authoritative)."""
    blob = sse_bytes(("text_delta", '{"delta":"Hi"}'), ("done", '{"text":"IGNORED"}'))
    response = FakeStreamResponse(chunk_bytes(blob, 6))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    deltas = await _collect(adapter, ctx)

    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]


async def test_error_event_raises_stream_error(hass: HomeAssistant) -> None:
    """An error event surfaces as BackendStreamError (guard turns it into fallback)."""
    response = FakeStreamResponse(chunk_bytes(sse_bytes(("error", '{"code":"x","message":"boom"}')), 5))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    with pytest.raises(BackendStreamError):
        await _collect(adapter, ctx)


async def test_error_after_deltas_raises_after_yielding(hass: HomeAssistant) -> None:
    """Deltas already streamed, then an error event → deltas seen, then raise."""
    blob = sse_bytes(("text_delta", '{"delta":"Hi"}'), ("error", '{"code":"x","message":"boom"}'))
    response = FakeStreamResponse(chunk_bytes(blob, 7))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    seen: list[Delta] = []
    with pytest.raises(BackendStreamError):
        async for delta in adapter.stream_turn(conversation.ChatLog(hass, "conv-1"), make_input("hello"), ctx):
            seen.append(delta)

    assert seen == [{"role": "assistant"}, {"content": "Hi"}]


async def test_silent_eof_raises(hass: HomeAssistant) -> None:
    """Stream ends with no done/error and no content → a dropped stream, not a reply (LLMM-025).

    The entity guard (LLMM-005) turns the error into the fallback.
    """
    response = FakeStreamResponse(chunk_bytes(b":keep-alive\n\n", 4))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    with pytest.raises(BackendStreamError, match="without done"):
        await _collect(adapter, ctx)


async def test_malformed_json_delta_skipped(hass: HomeAssistant) -> None:
    """A text_delta whose data is not valid JSON is skipped, not fatal."""
    blob = sse_bytes(("text_delta", "not json{"), ("text_delta", '{"delta":"Hi"}'), ("done", "{}"))
    response = FakeStreamResponse(chunk_bytes(blob, 4))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    deltas = await _collect(adapter, ctx)

    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]


async def test_http_error_status_raises_stream_error(hass: HomeAssistant) -> None:
    """A non-200 response raises BackendStreamError (guard → fallback)."""
    response = FakeStreamResponse([b""], status=500, text="upstream boom")
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    with pytest.raises(BackendStreamError):
        await _collect(adapter, ctx)


async def test_oversized_line_drained_then_stream_continues(hass: HomeAssistant) -> None:
    """A data line beyond the reader's cap is drained and skipped, never hangs; a
    following valid delta still streams instead of the whole turn aborting."""
    huge = "x" * 70000
    blob = sse_bytes(("text_delta", f'{{"delta":"{huge}"}}'), ("text_delta", '{"delta":"Hi"}'), ("done", "{}"))
    response = FakeStreamResponse(chunk_bytes(blob, 8192))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    deltas = await _collect(adapter, ctx)

    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]


def test_adapter_classvars() -> None:
    """Reference adapter contract: converse type, no HA tools, stateful memory scope."""
    assert ConverseAdapter.backend_type == "converse"
    assert ConverseAdapter.supports_ha_tools is False
    assert ConverseAdapter.supports_memory_scope is True


async def test_validate_connection_maps_client_error(hass: HomeAssistant) -> None:
    """A transport failure on the probe maps to BackendConnectionError."""
    from unittest.mock import MagicMock, patch

    import aiohttp

    from custom_components.llm_middleman.backends.base import BackendConnectionError

    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("refused"))
    with (
        patch(
            "custom_components.llm_middleman.backends.converse.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(BackendConnectionError),
    ):
        await ConverseAdapter.async_validate_connection(hass, {CONF_BASE_URL: TEST_BASE_URL})


async def test_turn_post_honors_agent_timeout(hass: HomeAssistant) -> None:
    # The per-agent CONF_TIMEOUT bounds both connect and silence on the wire call
    # (v0 hardcoded one 60 s total for everyone).
    blob = sse_bytes(("done", '{"text":""}'))
    session = fake_aiohttp_session(response=FakeStreamResponse([blob]))
    adapter = ConverseAdapter(hass, session, {CONF_BASE_URL: TEST_BASE_URL})
    await _collect(adapter, TurnContext(options={CONF_TIMEOUT: 90}, memory_key="k"))
    timeout = session.post.call_args.kwargs["timeout"]
    assert timeout.total is None
    assert timeout.sock_connect == 90
    assert timeout.sock_read == 90


# --- LLMM-025: stub-derived fixtures, terminal states, tool activity, probe ----

_FIXTURES = Path(__file__).parent / "fixtures" / "converse"


def _replay(name: str, *, width: int = 9) -> FakeStreamResponse:
    """The stub-recorded body, split at ``width`` bytes."""
    fx = cast("dict[str, Any]", json.loads((_FIXTURES / f"{name}.json").read_text()))
    return FakeStreamResponse(chunk_bytes(fx["body"].encode(), width), status=fx["status"])


def _spoken(deltas: list[Delta]) -> str:
    return "".join(str(delta.get("content") or "") for delta in deltas)


@pytest.mark.parametrize(
    ("fixture", "spoken"),
    [
        ("happy", "Hello from the converse stub, streaming one word at a time. "),
        ("follow_up", "Sure \u2014 should I turn on the lights as well? "),
        ("keepalive", "Hello from the converse stub, streaming one word at a time. "),
        ("tool_activity_late", "The kitchen lights are off now. "),
    ],
)
async def test_stub_reply_is_spoken(hass: HomeAssistant, fixture: str, spoken: str) -> None:
    deltas = await _collect(_adapter(hass, _replay(fixture)), TurnContext(options={}, memory_key="k"))
    assert deltas[0] == {"role": "assistant"}
    assert _spoken(deltas) == spoken


async def test_truncated_stream_raises_after_partial_text(hass: HomeAssistant) -> None:
    """EOF after deltas but without ``done`` is a dropped stream, never a complete reply."""
    adapter = _adapter(hass, _replay("truncated"))
    seen: list[Delta] = []
    with pytest.raises(BackendStreamError, match="without done"):
        async for delta in adapter.stream_turn(
            conversation.ChatLog(hass, "conv-1"), make_input(), TurnContext(options={}, memory_key="k")
        ):
            seen.append(delta)
    assert _spoken(seen) == "This reply is cut off mid-"


async def test_truncated_stream_through_entity_appends_fallback(
    hass: HomeAssistant, mock_chat_log: MockChatLog
) -> None:
    entity = build_entity(hass, _adapter(hass, _replay("truncated")), {})

    result = await entity._async_handle_message(make_input(), mock_chat_log)

    assert result.response.speech["plain"]["speech"] == f"This reply is cut off mid-\n{ERROR_MESSAGE}"
    assert result.response.error_code is not None


async def test_empty_done_through_entity_is_a_marked_fallback(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    entity = build_entity(hass, _adapter(hass, _replay("empty_done")), {})

    result = await entity._async_handle_message(make_input(), mock_chat_log)

    assert result.response.speech["plain"]["speech"] == ERROR_MESSAGE
    assert result.response.error_code is not None


async def test_error_event_code_is_logged(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(BackendStreamError, match="stub_error"):
        await _collect(_adapter(hass, _replay("error_event")), TurnContext(options={}, memory_key="k"))
    assert "stub-injected backend error" in caplog.text


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


async def test_tool_activity_before_text_surfaces_as_external_tool_calls(hass: HomeAssistant) -> None:
    deltas = await _collect(_adapter(hass, _replay("tool_activity_first")), TurnContext(options={}, memory_key="k"))
    assert _tool_events(deltas) == [
        ("call", "converse-tool-1", "HassTurnOff", {"name": "kitchen"}, True),
        ("result", "converse-tool-1", "HassTurnOff", {"status": "ok"}),
        ("call", "converse-tool-2", "GetWeather", {}, True),
        ("result", "converse-tool-2", "GetWeather", {"status": "error"}),
    ]


async def test_tool_activity_is_followed_by_a_spoken_reply_block(hass: HomeAssistant) -> None:
    deltas = await _collect(_adapter(hass, _replay("tool_activity_first")), TurnContext(options={}, memory_key="k"))
    reply = deltas[4:]
    assert reply[0] == {"role": "assistant"}
    assert _spoken(reply) == "The kitchen lights are off now. "


@pytest.mark.skipif(
    "result" not in conversation.ToolResultContentDeltaDict.__annotations__,
    reason="the tool-result error flag needs HA >= 2026.10",
)
async def test_tool_activity_non_ok_status_is_an_error_result(hass: HomeAssistant) -> None:
    deltas = await _collect(_adapter(hass, _replay("tool_activity_first")), TurnContext(options={}, memory_key="k"))
    raw = [cast("dict[str, Any]", delta) for delta in deltas]
    assert [delta["result"].error for delta in raw if delta.get("role") == "tool_result"] == [False, True]


async def test_tool_activity_id_field_is_used_when_present(hass: HomeAssistant) -> None:
    blob = sse_bytes(
        ("tool_activity", '{"id": "t-9", "tool": "Lights", "args": {}, "status": "ok"}'),
        ("text_delta", '{"delta": "Done."}'),
        ("done", "{}"),
    )
    deltas = await _collect(_adapter(hass, FakeStreamResponse([blob])), TurnContext(options={}, memory_key="k"))
    assert _tool_events(deltas)[0] == ("call", "t-9", "Lights", {}, True)


async def test_tool_activity_after_text_is_traced_not_shown(hass: HomeAssistant) -> None:
    # The spec example order: placing it after the text would end on a tool result or split the reply.
    with trace.async_conversation_trace() as conv_trace:
        deltas = await _collect(_adapter(hass, _replay("tool_activity_late")), TurnContext(options={}, memory_key="k"))
    details = [e["data"] for e in conv_trace.as_dict()["events"] if e["event_type"] == "agent_detail"]
    assert _tool_events(deltas) == []
    assert details == [
        {"converse_tool_activity": [{"tool": "HassTurnOff", "args": {"name": "kitchen"}, "status": "ok"}]}
    ]


async def test_tool_activity_with_done_text_only_is_shown_before_it(hass: HomeAssistant) -> None:
    blob = sse_bytes(("tool_activity", '{"tool": "Lights", "args": {}}'), ("done", '{"text": "Done."}'))
    deltas = await _collect(_adapter(hass, FakeStreamResponse([blob])), TurnContext(options={}, memory_key="k"))
    assert [event[0] for event in _tool_events(deltas)] == ["call", "result"]
    assert deltas[-2:] == [{"role": "assistant"}, {"content": "Done."}]


async def test_tool_activity_without_any_text_is_traced_and_falls_back(hass: HomeAssistant) -> None:
    blob = sse_bytes(("tool_activity", '{"tool": "Lights", "args": {}}'), ("done", "{}"))
    with trace.async_conversation_trace() as conv_trace:
        deltas = await _collect(_adapter(hass, FakeStreamResponse([blob])), TurnContext(options={}, memory_key="k"))
    details = [e["data"] for e in conv_trace.as_dict()["events"] if e["event_type"] == "agent_detail"]
    assert deltas == []
    assert details == [{"converse_tool_activity": [{"tool": "Lights", "args": {}}]}]


async def test_malformed_tool_activity_ignored(hass: HomeAssistant) -> None:
    blob = sse_bytes(
        ("tool_activity", "not json"),
        ("tool_activity", '{"args": {}}'),
        ("text_delta", '{"delta": "Hi"}'),
        ("done", "{}"),
    )
    deltas = await _collect(_adapter(hass, FakeStreamResponse([blob])), TurnContext(options={}, memory_key="k"))
    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]


async def test_turn_with_tool_activity_posts_once(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """External tool results must not re-enter the entity's HA tool loop (one POST per turn)."""
    adapter = _adapter(hass, _replay("tool_activity_first"))
    entity = build_entity(hass, adapter, {})

    result = await entity._async_handle_message(make_input(), mock_chat_log)

    assert cast("MagicMock", adapter.session).post.call_count == 1
    assert result.response.speech["plain"]["speech"] == "The kitchen lights are off now. "


def _probe(hass: HomeAssistant, status: int) -> Any:
    session = MagicMock()
    session.get = MagicMock(return_value=FakeStreamResponse([b""], status=status))
    return patch("custom_components.llm_middleman.backends.converse.async_get_clientsession", return_value=session)


@pytest.mark.parametrize("status", [401, 403])
async def test_validate_connection_rejected_token_is_invalid_auth(hass: HomeAssistant, status: int) -> None:
    with _probe(hass, status), pytest.raises(BackendAuthError):
        await ConverseAdapter.async_validate_connection(hass, {CONF_BASE_URL: TEST_BASE_URL, CONF_TOKEN: "bad"})


@pytest.mark.parametrize("status", [200, 404, 405, 500])
async def test_validate_connection_other_status_is_reachable(hass: HomeAssistant, status: int) -> None:
    # converse defines only POST /v1/converse, so GET on the base URL routinely 404s/405s.
    with _probe(hass, status):
        await ConverseAdapter.async_validate_connection(hass, {CONF_BASE_URL: TEST_BASE_URL})


async def test_error_event_without_code_never_says_none(hass: HomeAssistant) -> None:
    response = FakeStreamResponse([sse_bytes(("error", '{"message": "boom"}'))])
    with pytest.raises(BackendStreamError) as err:
        await _collect(_adapter(hass, response), TurnContext(options={}, memory_key="k"))
    assert "None" not in str(err.value)
