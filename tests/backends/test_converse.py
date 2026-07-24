"""Tests for the custom ``/v1/converse`` adapter (LLMM-009).

Drives **raw bytes** through the real ``_sse`` reader and the adapter — never v0's
pre-split ``_FakeContent`` lines. Exercises the surfaces v0 left untested: multi-line
``data:`` framing, mid-frame chunk splits, ``done`` without a delta, ``error`` after
deltas, silent EOF, malformed JSON, and oversized lines.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant

from custom_components.llm_middleman.backends._sse import BackendStreamError
from custom_components.llm_middleman.backends.base import TurnContext
from custom_components.llm_middleman.backends.converse import ConverseAdapter
from custom_components.llm_middleman.const import (
    CONF_BASE_URL,
    CONF_TIMEOUT,
    CONF_TOKEN,
    CONVERSE_PATH,
    IDLE_TIMEOUT,
)
from tests.conftest import (
    TEST_BASE_URL,
    TEST_TOKEN,
    FakeStreamResponse,
    chunk_bytes,
    fake_aiohttp_session,
    sse_bytes,
)

Delta = conversation.AssistantContentDeltaDict


def _make_input(*, device_id: str | None = None) -> conversation.ConversationInput:
    return conversation.ConversationInput(
        text="hello",
        context=Context(),
        conversation_id="conv-1",
        device_id=device_id,
        satellite_id=None,
        language="en",
        agent_id="agent-1",
    )


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
    return [
        delta
        async for delta in adapter.stream_turn(object(), _make_input(device_id=device_id), ctx)  # type: ignore[arg-type]
    ]


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


async def test_done_without_delta_or_text_falls_back(hass: HomeAssistant) -> None:
    """done with neither delta nor text voices the canonical fallback message."""
    from custom_components.llm_middleman.const import ERROR_MESSAGE

    response = FakeStreamResponse(chunk_bytes(sse_bytes(("done", "{}")), 4))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    deltas = await _collect(adapter, ctx)

    assert deltas == [{"role": "assistant"}, {"content": ERROR_MESSAGE}]


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
        async for delta in adapter.stream_turn(object(), _make_input(), ctx):  # type: ignore[arg-type]
            seen.append(delta)

    assert seen == [{"role": "assistant"}, {"content": "Hi"}]


async def test_silent_eof_yields_nothing(hass: HomeAssistant) -> None:
    """Stream ends with no done/error and no content → adapter yields nothing.

    The entity guard (LLMM-005), not the adapter, supplies the fallback.
    """
    response = FakeStreamResponse(chunk_bytes(b":keep-alive\n\n", 4))
    adapter = _adapter(hass, response)
    ctx = TurnContext(options={}, memory_key="k")

    deltas = await _collect(adapter, ctx)

    assert deltas == []
    assert ctx.continue_conversation is False


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
    # The per-agent CONF_TIMEOUT plus the shared idle deadline reach the wire call
    # (v0 hardcoded one 60 s total for everyone).
    blob = sse_bytes(("done", '{"text":""}'))
    session = fake_aiohttp_session(response=FakeStreamResponse([blob]))
    adapter = ConverseAdapter(hass, session, {CONF_BASE_URL: TEST_BASE_URL})
    await _collect(adapter, TurnContext(options={CONF_TIMEOUT: 90}, memory_key="k"))
    timeout = session.post.call_args.kwargs["timeout"]
    assert timeout.total == 90
    assert timeout.sock_read == IDLE_TIMEOUT
