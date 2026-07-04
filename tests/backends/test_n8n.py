"""Tests for the n8n backend adapter (LLMM-012).

Raw bytes are driven through the *real* parsers via the conftest stream harness
(``FakeStreamResponse`` + ``fake_aiohttp_session``), splitting NDJSON chunks at
arbitrary boundaries. Covers the dual streaming/blocking branch (on the actual
response, not the config toggle), multiple begin/end cycles, EOF-as-done, error and
HTML fallbacks, the output-field fallback chain, misconfig visibility, auth, and
credential redaction.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant

from custom_components.llm_middleman.backends import BACKEND_TO_CLS
from custom_components.llm_middleman.backends.base import (
    BackendConnectionError,
    BackendStreamError,
    TurnContext,
)
from custom_components.llm_middleman.backends.n8n import N8nAdapter
from custom_components.llm_middleman.const import (
    BACKEND_N8N,
    CONF_INPUT_FIELD,
    CONF_N8N_AUTH_TYPE,
    CONF_N8N_HEADER_NAME,
    CONF_N8N_HEADER_VALUE,
    CONF_N8N_PASSWORD,
    CONF_N8N_USERNAME,
    CONF_OUTPUT_FIELD,
    CONF_SESSION_FIELD,
    CONF_STREAMING,
    CONF_SYSTEM_PROMPT,
    CONF_TARGET_TYPE,
    CONF_WEBHOOK_URL,
    N8N_AUTH_BASIC,
    N8N_AUTH_HEADER,
    TARGET_CHAT_TRIGGER,
    TARGET_PLAIN_WEBHOOK,
)
from tests.conftest import FakeStreamResponse, chunk_bytes, fake_aiohttp_session

_WEBHOOK = "https://n8n.local/webhook/abc/chat"


def _make_input() -> conversation.ConversationInput:
    return conversation.ConversationInput(
        text="hi there",
        context=Context(),
        conversation_id="conv-1",
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="agent-1",
    )


def _adapter(
    hass: HomeAssistant,
    session: Any,
    *,
    connection: dict[str, Any] | None = None,
) -> N8nAdapter:
    data: dict[str, Any] = {CONF_WEBHOOK_URL: _WEBHOOK}
    if connection is not None:
        data.update(connection)
    return N8nAdapter(hass, cast("aiohttp.ClientSession", session), data)


async def _collect(
    adapter: N8nAdapter,
    ctx: TurnContext,
) -> list[conversation.AssistantContentDeltaDict]:
    chat_log = cast("conversation.ChatLog", object())
    return [delta async for delta in adapter.stream_turn(chat_log, _make_input(), ctx)]


def _ctx(**options: Any) -> TurnContext:
    return TurnContext(options=options, memory_key="conv-1")


def _response(chunks: list[bytes], content_type: str, *, status: int = 200) -> FakeStreamResponse:
    return FakeStreamResponse(chunks, status=status, headers={"Content-Type": content_type})


# --- streaming (NDJSON) ------------------------------------------------------


async def test_streaming_happy_path_split_mid_object(hass: HomeAssistant) -> None:
    blob = b'{"type":"begin"}\n{"type":"item","content":"Hel"}\n{"type":"item","content":"lo"}\n{"type":"end"}\n'
    # Fixed width 7 splits every object across chunk boundaries; content-type is plain
    # application/json, so the ndjson branch must be chosen from the first-line shape.
    response = _response(chunk_bytes(blob, 7), "application/json")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "Hel"}, {"content": "lo"}]


async def test_multiple_begin_end_cycles(hass: HomeAssistant) -> None:
    blob = (
        b'{"type":"begin"}\n{"type":"item","content":"A"}\n{"type":"end"}\n'
        b'{"type":"begin"}\n{"type":"item","content":"B"}\n{"type":"end"}\n'
    )
    response = _response(chunk_bytes(blob, 9), "application/json-lines")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    # The first `end` is not terminal; both items are emitted, single leading role.
    assert deltas == [{"role": "assistant"}, {"content": "A"}, {"content": "B"}]


async def test_content_whitespace_not_stripped(hass: HomeAssistant) -> None:
    blob = b'{"type":"begin"}\n{"type":"item","content":" hi "}\n{"type":"end"}\n'
    response = _response([blob], "application/x-ndjson")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": " hi "}]


async def test_malformed_json_line_skipped(hass: HomeAssistant) -> None:
    blob = b'{"type":"begin"}\n{not valid json}\n{"type":"item","content":"ok"}\n{"type":"end"}\n'
    response = _response([blob], "application/json-lines")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "ok"}]


async def test_silent_stream_end_raises(hass: HomeAssistant) -> None:
    # begin/end with no item content, then EOF -> nothing usable produced.
    blob = b'{"type":"begin"}\n{"type":"end"}\n'
    response = _response([blob], "application/json-lines")
    session = fake_aiohttp_session(response=response)
    with pytest.raises(BackendStreamError):
        await _collect(_adapter(hass, session), _ctx())


async def test_error_chunk_raises(hass: HomeAssistant) -> None:
    blob = b'{"type":"begin"}\n{"type":"error","content":"boom"}\n'
    response = _response([blob], "application/json-lines")
    session = fake_aiohttp_session(response=response)
    with pytest.raises(BackendStreamError, match="boom"):
        await _collect(_adapter(hass, session), _ctx())


async def test_html_body_raises(hass: HomeAssistant) -> None:
    blob = b"<html><body><h1>504 Gateway Time-out</h1></body></html>"
    response = _response([blob], "text/html")
    session = fake_aiohttp_session(response=response)
    with pytest.raises(BackendStreamError):
        await _collect(_adapter(hass, session), _ctx())


# --- blocking / wrong-mode ---------------------------------------------------


async def test_wrong_mode_mismatch_streaming_toggle_but_blocking_body(hass: HomeAssistant) -> None:
    # `streaming` config is ON, but n8n sent a single blocking JSON body: the adapter
    # must detect the real response and NOT wait for NDJSON deltas.
    response = _response([b'{"output":"done"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx(**{CONF_STREAMING: True}))
    assert deltas == [{"role": "assistant"}, {"content": "done"}]


async def test_blocking_output_fallback_to_text(hass: HomeAssistant) -> None:
    # Default output_field is `output`; absent -> falls back to `text`.
    response = _response([b'{"text":"hi"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "hi"}]


async def test_blocking_custom_output_field(hass: HomeAssistant) -> None:
    response = _response([b'{"answer":"yo"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx(**{CONF_OUTPUT_FIELD: "answer"}))
    assert deltas == [{"role": "assistant"}, {"content": "yo"}]


async def test_blocking_missing_output_field_surfaces_error(hass: HomeAssistant) -> None:
    # Neither `output` nor `text` present -> surfaced error, never speak the raw object.
    response = _response([b'{"foo":"bar"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    with pytest.raises(BackendStreamError):
        await _collect(_adapter(hass, session), _ctx())


async def test_blocking_pretty_printed_json(hass: HomeAssistant) -> None:
    # A pretty-printed blocking body has newlines but no StructuredChunk first line.
    response = _response([b'{\n  "output": "spread"\n}'], "application/json")
    session = fake_aiohttp_session(response=response)
    deltas = await _collect(_adapter(hass, session), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "spread"}]


async def test_blocking_continue_conversation(hass: HomeAssistant) -> None:
    response = _response([b'{"output":"more?","continueConversation":true}'], "application/json")
    session = fake_aiohttp_session(response=response)
    ctx = _ctx()
    deltas = await _collect(_adapter(hass, session), ctx)
    assert deltas == [{"role": "assistant"}, {"content": "more?"}]
    assert ctx.continue_conversation is True


# --- request shape -----------------------------------------------------------


def _posted_body(session: Any) -> dict[str, Any]:
    call = session.post.call_args
    return cast("dict[str, Any]", json.loads(call.kwargs["data"]))


async def test_action_included_for_chat_trigger(hass: HomeAssistant) -> None:
    response = _response([b'{"output":"ok"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    await _collect(
        _adapter(hass, session, connection={CONF_TARGET_TYPE: TARGET_CHAT_TRIGGER}),
        _ctx(),
    )
    body = _posted_body(session)
    assert body["action"] == "sendMessage"
    # Session key and turn text are sent under the default field names.
    assert body["sessionId"] == "conv-1"
    assert body["chatInput"] == "hi there"
    # Default per-turn deadline is the n8n-specific 30 s (lower than the global default).
    assert session.post.call_args.kwargs["timeout"].total == 30


async def test_action_omitted_for_plain_webhook(hass: HomeAssistant) -> None:
    response = _response([b'{"output":"ok"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    await _collect(
        _adapter(hass, session, connection={CONF_TARGET_TYPE: TARGET_PLAIN_WEBHOOK}),
        _ctx(),
    )
    assert "action" not in _posted_body(session)


async def test_custom_fields_and_system_prompt(hass: HomeAssistant) -> None:
    response = _response([b'{"output":"ok"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    await _collect(
        _adapter(hass, session),
        _ctx(
            **{
                CONF_SESSION_FIELD: "sid",
                CONF_INPUT_FIELD: "prompt",
                CONF_SYSTEM_PROMPT: "be terse",
            }
        ),
    )
    body = _posted_body(session)
    assert body["sid"] == "conv-1"
    assert body["prompt"] == "hi there"
    assert body["systemPrompt"] == "be terse"


# --- auth + redaction --------------------------------------------------------


async def test_auth_basic_sets_basicauth_and_redacts(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    response = _response([b'{"output":"ok"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    with caplog.at_level(logging.DEBUG, logger="custom_components.llm_middleman.backends.n8n"):
        await _collect(
            _adapter(
                hass,
                session,
                connection={
                    CONF_N8N_AUTH_TYPE: N8N_AUTH_BASIC,
                    CONF_N8N_USERNAME: "user",
                    CONF_N8N_PASSWORD: "s3cret",
                },
            ),
            _ctx(),
        )
    auth = session.post.call_args.kwargs["auth"]
    assert isinstance(auth, aiohttp.BasicAuth)
    assert auth == aiohttp.BasicAuth("user", "s3cret")
    assert "s3cret" not in caplog.text


async def test_auth_header_sets_header_and_redacts(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    response = _response([b'{"output":"ok"}'], "application/json")
    session = fake_aiohttp_session(response=response)
    with caplog.at_level(logging.DEBUG, logger="custom_components.llm_middleman.backends.n8n"):
        await _collect(
            _adapter(
                hass,
                session,
                connection={
                    CONF_N8N_AUTH_TYPE: N8N_AUTH_HEADER,
                    CONF_N8N_HEADER_NAME: "X-Api-Key",
                    CONF_N8N_HEADER_VALUE: "tok-42",
                },
            ),
            _ctx(),
        )
    headers = session.post.call_args.kwargs["headers"]
    assert headers["X-Api-Key"] == "tok-42"
    assert session.post.call_args.kwargs["auth"] is None
    assert "tok-42" not in caplog.text


# --- registration + validation ----------------------------------------------


def test_registered_in_factory() -> None:
    assert BACKEND_TO_CLS[BACKEND_N8N] is N8nAdapter
    assert N8nAdapter.backend_type == BACKEND_N8N
    assert N8nAdapter.supports_ha_tools is False
    assert N8nAdapter.supports_memory_scope is True


async def test_validate_connection_accepts_valid_url(hass: HomeAssistant) -> None:
    await N8nAdapter.async_validate_connection(hass, {CONF_WEBHOOK_URL: _WEBHOOK})


@pytest.mark.parametrize("url", ["", "not-a-url", "ftp://n8n.local/webhook"])
async def test_validate_connection_rejects_bad_url(hass: HomeAssistant, url: str) -> None:
    with pytest.raises(BackendConnectionError):
        await N8nAdapter.async_validate_connection(hass, {CONF_WEBHOOK_URL: url})
