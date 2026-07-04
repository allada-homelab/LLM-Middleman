"""Tests for the LLM Middleman conversation entity."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL

from custom_components.llm_middleman.const import (
    CONF_TOKEN,
    CONF_URL,
    DOMAIN,
    ERROR_MESSAGE,
)
from custom_components.llm_middleman.conversation import LLMMiddlemanConversationEntity

from .conftest import MockChatLog

TEST_URL = "http://middleman.local:8000"


class _FakeContent:
    """Async-iterable stand-in for aiohttp response.content (yields byte lines)."""

    def __init__(self, lines: Iterable[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self):
        async def _gen():
            for line in self._lines:
                yield line

        return _gen()


class _FakeResponse:
    """Async-context-manager stand-in for an aiohttp streaming response."""

    def __init__(self, *, status: int = 200, lines: Iterable[bytes] = (), text: str = "") -> None:
        self.status = status
        self.content = _FakeContent(lines)
        self._text = text

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _make_session(*, response: _FakeResponse | None = None, exc: Exception | None = None) -> MagicMock:
    session = MagicMock()
    if exc is not None:
        session.post = MagicMock(side_effect=exc)
    else:
        session.post = MagicMock(return_value=response)
    return session


def _make_entity(session: MagicMock, data: dict[str, Any] | None = None) -> LLMMiddlemanConversationEntity:
    entry = MagicMock()
    entry.entry_id = "test-entry-id"
    entry.title = "Test Middleman"
    entry.data = data if data is not None else {CONF_URL: TEST_URL, CONF_TOKEN: "test-token"}
    entry.runtime_data = session
    entity = LLMMiddlemanConversationEntity(entry)
    entity.entity_id = "conversation.test"
    return entity


def _sse(*events: tuple[str, str]) -> list[bytes]:
    """Build SSE byte lines from (event, json_data) pairs."""
    lines: list[bytes] = []
    for event, data in events:
        lines.append(f"event: {event}\n".encode())
        lines.append(f"data: {data}\n".encode())
        lines.append(b"\n")
    return lines


def test_supported_languages() -> None:
    """All languages are supported (the external agent handles language)."""
    entity = _make_entity(_make_session())
    assert entity.supported_languages == MATCH_ALL


def test_supports_streaming() -> None:
    """Streaming is enabled so TTS can start early."""
    entity = _make_entity(_make_session())
    assert entity.supports_streaming is True


def test_unique_id_and_device_info() -> None:
    """Unique id and device info derive from the config entry."""
    entity = _make_entity(_make_session())
    assert entity.unique_id == "test-entry-id"
    assert entity.device_info is not None
    assert (DOMAIN, "test-entry-id") in entity.device_info["identifiers"]  # pyright: ignore[reportTypedDictNotRequiredAccess]  # LLMM-005 replaces this
    assert entity.device_info["name"] == "Test Middleman"  # pyright: ignore[reportTypedDictNotRequiredAccess]  # LLMM-005 replaces this


async def test_forward_streams_deltas_into_chat_log(mock_chat_log: MockChatLog) -> None:
    """text_delta events are concatenated into a single AssistantContent."""
    response = _FakeResponse(
        lines=_sse(
            ("text_delta", '{"delta": "Turning off "}'),
            ("text_delta", '{"delta": "the kitchen lights."}'),
            ("done", '{"text": "Turning off the kitchen lights.", "continue_conversation": false}'),
        )
    )
    entity = _make_entity(_make_session(response=response))

    user_input = MagicMock(spec=conversation.ConversationInput)
    user_input.text = "turn off the kitchen lights"
    user_input.language = "en"
    user_input.device_id = "device-1"

    await entity._async_forward(user_input, mock_chat_log)

    last = mock_chat_log.content[-1]
    assert isinstance(last, conversation.AssistantContent)
    assert last.content == "Turning off the kitchen lights."


async def test_forward_sends_auth_and_body(mock_chat_log: MockChatLog) -> None:
    """The request carries the bearer token and the converse contract body."""
    session = _make_session(response=_FakeResponse(lines=_sse(("done", '{"text": "ok"}'))))
    entity = _make_entity(session, data={CONF_URL: TEST_URL, CONF_TOKEN: "secret"})

    user_input = MagicMock(spec=conversation.ConversationInput)
    user_input.text = "hello"
    user_input.language = "en"
    user_input.device_id = None

    await entity._async_forward(user_input, mock_chat_log)

    args, kwargs = session.post.call_args
    assert args[0] == f"{TEST_URL}/v1/converse"
    assert kwargs["headers"]["Authorization"] == "Bearer secret"
    assert kwargs["json"]["text"] == "hello"
    assert kwargs["json"]["language"] == "en"
    # device_id omitted when not provided
    assert "device_id" not in kwargs["json"]


async def test_forward_error_event_falls_back(mock_chat_log: MockChatLog) -> None:
    """An error event surfaces a graceful assistant message, not a hang."""
    response = _FakeResponse(lines=_sse(("error", '{"code": "backend_unavailable", "message": "down"}')))
    entity = _make_entity(_make_session(response=response))

    user_input = MagicMock(spec=conversation.ConversationInput)
    user_input.text = "hi"
    user_input.language = "en"
    user_input.device_id = None

    await entity._async_forward(user_input, mock_chat_log)

    last = mock_chat_log.content[-1]
    assert isinstance(last, conversation.AssistantContent)
    assert last.content == ERROR_MESSAGE


async def test_forward_timeout_falls_back(mock_chat_log: MockChatLog) -> None:
    """A timeout surfaces the fallback message rather than raising."""
    entity = _make_entity(_make_session(exc=TimeoutError()))

    user_input = MagicMock(spec=conversation.ConversationInput)
    user_input.text = "hi"
    user_input.language = "en"
    user_input.device_id = None

    await entity._async_forward(user_input, mock_chat_log)

    last = mock_chat_log.content[-1]
    assert isinstance(last, conversation.AssistantContent)
    assert last.content == ERROR_MESSAGE


async def test_forward_http_error_falls_back(mock_chat_log: MockChatLog) -> None:
    """A non-200 response surfaces the fallback message."""
    response = _FakeResponse(status=502, text="bad gateway")
    entity = _make_entity(_make_session(response=response))

    user_input = MagicMock(spec=conversation.ConversationInput)
    user_input.text = "hi"
    user_input.language = "en"
    user_input.device_id = None

    await entity._async_forward(user_input, mock_chat_log)

    last = mock_chat_log.content[-1]
    assert isinstance(last, conversation.AssistantContent)
    assert last.content == ERROR_MESSAGE


@pytest.mark.parametrize("device_id", ["voice-satellite-1", None])
async def test_forward_includes_device_id_when_present(mock_chat_log: MockChatLog, device_id: str | None) -> None:
    """device_id is forwarded only when the turn originated from a device."""
    session = _make_session(response=_FakeResponse(lines=_sse(("done", '{"text": "ok"}'))))
    entity = _make_entity(session)

    user_input = MagicMock(spec=conversation.ConversationInput)
    user_input.text = "hi"
    user_input.language = "en"
    user_input.device_id = device_id

    await entity._async_forward(user_input, mock_chat_log)

    body = session.post.call_args.kwargs["json"]
    assert body.get("device_id") == device_id if device_id else "device_id" not in body


# --- _async_handle_message orchestration ---


async def test_handle_message_runs_chain_and_returns_result() -> None:
    """The turn chain: provide_llm_data (no HA tools) -> forward -> build result."""
    entity = _make_entity(_make_session())
    entity.hass = MagicMock()

    user_input = MagicMock(spec=conversation.ConversationInput)
    user_input.extra_system_prompt = None
    user_input.as_llm_context.return_value = MagicMock()

    chat_log = MagicMock(spec=conversation.ChatLog)
    chat_log.async_provide_llm_data = AsyncMock()

    with (
        patch.object(entity, "_async_forward", new_callable=AsyncMock) as mock_forward,
        patch(
            "custom_components.llm_middleman.conversation.conversation.async_get_result_from_chat_log",
            return_value=MagicMock(spec=conversation.ConversationResult),
        ) as mock_result,
    ):
        result = await entity._async_handle_message(user_input, chat_log)

    # No HA LLM API is provided — the external agent owns tool calling.
    assert chat_log.async_provide_llm_data.call_args[0][1] is None
    mock_forward.assert_awaited_once_with(user_input, chat_log)
    mock_result.assert_called_once_with(user_input, chat_log)
    assert isinstance(result, conversation.ConversationResult)


async def test_handle_message_converse_error_is_returned() -> None:
    """A pre-flight ConverseError is surfaced as a ConversationResult, not raised."""
    entity = _make_entity(_make_session())
    entity.hass = MagicMock()

    user_input = MagicMock(spec=conversation.ConversationInput)
    user_input.extra_system_prompt = None
    user_input.as_llm_context.return_value = MagicMock()

    chat_log = MagicMock(spec=conversation.ChatLog)
    chat_log.async_provide_llm_data = AsyncMock(side_effect=conversation.ConverseError("boom", "conv-1", MagicMock()))

    with patch.object(entity, "_async_forward", new_callable=AsyncMock) as mock_forward:
        result = await entity._async_handle_message(user_input, chat_log)

    assert isinstance(result, conversation.ConversationResult)
    mock_forward.assert_not_called()
