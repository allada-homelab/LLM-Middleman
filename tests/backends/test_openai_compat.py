"""Raw-byte tests for the OpenAI-compatible adapter (LLMM-008).

Drives the *real* ``_sse`` reader + adapter with fake aiohttp streams whose chunk
boundaries fall mid-line and mid-frame (byte-at-a-time / arbitrary offsets, CRLF),
never pre-split lines. Covers the plan's fallback surfaces: done-with-no-delta,
silent stream end, error-after-deltas, oversized line, malformed JSON.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.llm_middleman.backends import BACKEND_TO_CLS, get_backend_cls
from custom_components.llm_middleman.backends._sse import BackendStreamError
from custom_components.llm_middleman.backends.base import (
    BackendAuthError,
    BackendConnectionError,
    TurnContext,
)
from custom_components.llm_middleman.backends.openai_compat import OpenAICompatAdapter
from custom_components.llm_middleman.const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MAX_HISTORY,
    CONF_MAX_TOKENS,
    CONF_MODEL,
    CONF_TEMPERATURE,
    CONF_TOP_P,
)
from tests.conftest import (
    TEST_API_KEY,
    TEST_BASE_URL,
    FakeStreamResponse,
    MockChatLog,
    chunk_bytes,
    fake_aiohttp_session,
)

_CONNECTION = {CONF_BASE_URL: TEST_BASE_URL, CONF_API_KEY: TEST_API_KEY}


def _data_frame(delta: dict[str, Any]) -> bytes:
    """One SSE ``data:`` frame carrying an OpenAI chat-completions chunk, CRLF-framed."""
    payload = json.dumps({"choices": [{"delta": delta}]})
    return b"data: " + payload.encode() + b"\r\n\r\n"


def _input() -> conversation.ConversationInput:
    return conversation.ConversationInput(
        text="hi",
        context=Context(),
        conversation_id="conv-1",
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="agent-1",
    )


def _adapter(hass: HomeAssistant, session: Any) -> OpenAICompatAdapter:
    return OpenAICompatAdapter(hass, session, _CONNECTION)


async def _collect(
    hass: HomeAssistant,
    chat_log: conversation.ChatLog,
    chunks: list[bytes],
    ctx: TurnContext,
    *,
    raise_after: int | None = None,
    exc: Exception | None = None,
) -> list[conversation.AssistantContentDeltaDict]:
    response = FakeStreamResponse(chunks, raise_after=raise_after, exc=exc)
    adapter = _adapter(hass, fake_aiohttp_session(response=response))
    return [delta async for delta in adapter.stream_turn(chat_log, _input(), ctx)]


def _ctx(**options: Any) -> TurnContext:
    return TurnContext(options=options, memory_key="k")


# --- streaming happy path -------------------------------------------------------


async def test_happy_path_role_first_split_mid_frame(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # role-only chunk (content is None) is skipped; adapter emits its own role delta.
    blob = (
        _data_frame({"role": "assistant"})
        + _data_frame({"content": "Hel"})
        + _data_frame({"content": "lo"})
        + b"data: [DONE]\r\n\r\n"
    )
    # byte-at-a-time: boundaries fall mid-line and mid-`data:` frame.
    deltas = await _collect(hass, mock_chat_log, chunk_bytes(blob, 1), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "Hel"}, {"content": "lo"}]
    assert deltas[0] == {"role": "assistant"}  # role-first


async def test_whitespace_preserved(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    blob = _data_frame({"content": "Hello"}) + _data_frame({"content": " world"}) + b"data: [DONE]\n\n"
    deltas = await _collect(hass, mock_chat_log, [blob], _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "Hello"}, {"content": " world"}]


async def test_empty_string_delta_passes_through(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    blob = _data_frame({"content": ""}) + _data_frame({"content": "x"}) + b"data: [DONE]\n\n"
    deltas = await _collect(hass, mock_chat_log, [blob], _ctx())
    assert deltas == [{"role": "assistant"}, {"content": ""}, {"content": "x"}]


# --- fallback surfaces ----------------------------------------------------------


async def test_no_done_eof_terminates(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # silent stream end: content deltas but no [DONE] sentinel — EOF still ends it.
    blob = _data_frame({"content": "Hel"}) + _data_frame({"content": "lo"})
    deltas = await _collect(hass, mock_chat_log, chunk_bytes(blob, 1), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "Hel"}, {"content": "lo"}]
    text = "".join(str(d.get("content", "")) for d in deltas if d.get("content"))
    assert text == "Hello"


async def test_done_with_no_delta_yields_nothing(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    blob = _data_frame({}) + b"data: [DONE]\n\n"
    deltas = await _collect(hass, mock_chat_log, [blob], _ctx())
    assert deltas == []


async def test_error_after_deltas_propagates(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    chunks = [_data_frame({"content": "Hi"}), _data_frame({"content": "!"})]
    adapter = _adapter(
        hass, fake_aiohttp_session(response=FakeStreamResponse(chunks, raise_after=1, exc=aiohttp.ClientError()))
    )
    deltas: list[conversation.AssistantContentDeltaDict] = []
    with pytest.raises(aiohttp.ClientError):
        async for delta in adapter.stream_turn(mock_chat_log, _input(), _ctx()):
            deltas.append(delta)
    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]


async def test_oversized_line_raises_backend_stream_error(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    blob = b"data: " + b"x" * 70000 + b"\n\n"
    with pytest.raises(BackendStreamError):
        await _collect(hass, mock_chat_log, [blob], _ctx())


async def test_malformed_json_raises(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    with pytest.raises(json.JSONDecodeError):
        await _collect(hass, mock_chat_log, [b"data: not-json\n\n"], _ctx())


# --- history replay + options ---------------------------------------------------


def _posted_body(session: MagicMock) -> dict[str, Any]:
    return session.post.call_args.kwargs["json"]


async def test_history_trim_system_plus_last_three(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    content: list[conversation.Content] = [
        conversation.SystemContent(content="sys"),
        conversation.UserContent(content="u1"),
        conversation.AssistantContent(agent_id="a", content="a1"),
        conversation.UserContent(content="u2"),
        conversation.AssistantContent(agent_id="a", content="a2"),
        conversation.UserContent(content="u3"),
    ]
    mock_chat_log.content = content
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = _adapter(hass, session)
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx(**{CONF_MAX_HISTORY: 1}))]
    messages = _posted_body(session)["messages"]
    assert messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]


async def test_history_untrimmed_when_max_history_zero(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    content: list[conversation.Content] = [
        conversation.SystemContent(content="sys"),
        conversation.UserContent(content="u1"),
        conversation.AssistantContent(agent_id="a", content="a1"),
        conversation.UserContent(content="u2"),
    ]
    mock_chat_log.content = content
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = _adapter(hass, session)
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx())]
    assert len(_posted_body(session)["messages"]) == 4


async def test_options_present_only_when_set(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = _adapter(hass, session)
    ctx = _ctx(**{CONF_MODEL: "m", CONF_TEMPERATURE: 0.5, CONF_TOP_P: 0.9, CONF_MAX_TOKENS: 100})
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), ctx)]
    body = _posted_body(session)
    assert body["model"] == "m"
    assert body["temperature"] == 0.5
    assert body["top_p"] == 0.9
    assert body["max_tokens"] == 100
    assert body["stream"] is True


async def test_options_omitted_when_unset(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = _adapter(hass, session)
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx(**{CONF_MODEL: "m"}))]
    body = _posted_body(session)
    assert body["model"] == "m"
    assert "temperature" not in body
    assert "top_p" not in body
    assert "max_tokens" not in body


async def test_trailing_slash_and_bearer_header(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = OpenAICompatAdapter(hass, session, {CONF_BASE_URL: TEST_BASE_URL + "/", CONF_API_KEY: "sekret"})
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx())]
    assert session.post.call_args.args[0] == f"{TEST_BASE_URL}/v1/chat/completions"
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer sekret"


async def test_no_bearer_header_without_api_key(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = OpenAICompatAdapter(hass, session, {CONF_BASE_URL: TEST_BASE_URL})
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx())]
    assert "Authorization" not in session.post.call_args.kwargs["headers"]


# --- registration ---------------------------------------------------------------


def test_registered_in_factory() -> None:
    assert BACKEND_TO_CLS["openai_compat"] is OpenAICompatAdapter
    assert get_backend_cls("openai_compat") is OpenAICompatAdapter
    assert OpenAICompatAdapter.backend_type == "openai_compat"
    assert OpenAICompatAdapter.supports_ha_tools is False


# --- connection probe / model list ----------------------------------------------


class _FakeGetResponse:
    """Minimal ``GET`` response stand-in with ``status`` + async ``json()``."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def json(self) -> Any:
        return self._payload

    async def __aenter__(self) -> _FakeGetResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _get_session(*, response: _FakeGetResponse | None = None, exc: Exception | None = None) -> MagicMock:
    session = MagicMock()
    if exc is not None:
        session.get = MagicMock(side_effect=exc)
    else:
        session.get = MagicMock(return_value=response)
    return session


def _patch_session(session: MagicMock) -> Any:
    return patch(
        "custom_components.llm_middleman.backends.openai_compat.async_get_clientsession",
        return_value=session,
    )


async def test_validate_connection_ok(hass: HomeAssistant) -> None:
    session = _get_session(response=_FakeGetResponse(200, {"data": []}))
    with _patch_session(session):
        assert await OpenAICompatAdapter.async_validate_connection(hass, _CONNECTION) is None
    assert session.get.call_args.args[0] == f"{TEST_BASE_URL}/v1/models"


async def test_validate_connection_auth_error(hass: HomeAssistant) -> None:
    session = _get_session(response=_FakeGetResponse(401, {}))
    with _patch_session(session), pytest.raises(BackendAuthError):
        await OpenAICompatAdapter.async_validate_connection(hass, _CONNECTION)


async def test_validate_connection_server_error(hass: HomeAssistant) -> None:
    session = _get_session(response=_FakeGetResponse(500, {}))
    with _patch_session(session), pytest.raises(BackendConnectionError):
        await OpenAICompatAdapter.async_validate_connection(hass, _CONNECTION)


async def test_validate_connection_transport_error(hass: HomeAssistant) -> None:
    session = _get_session(exc=aiohttp.ClientError())
    with _patch_session(session), pytest.raises(BackendConnectionError):
        await OpenAICompatAdapter.async_validate_connection(hass, _CONNECTION)


async def test_list_models_parses_ids(hass: HomeAssistant) -> None:
    payload = {"data": [{"id": "gpt-4"}, {"id": "llama-3"}, {"object": "no-id"}]}
    session = _get_session(response=_FakeGetResponse(200, payload))
    with _patch_session(session):
        models = await OpenAICompatAdapter.async_list_models(hass, _CONNECTION)
    assert models == ["gpt-4", "llama-3"]


async def test_real_session_type_contract(hass: HomeAssistant) -> None:
    # The constructor stores the shared session unchanged (parity with base tests).
    session = async_get_clientsession(hass)
    adapter = OpenAICompatAdapter(hass, session, _CONNECTION)
    assert adapter.session is session
