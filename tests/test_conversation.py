"""Tests for the backend-agnostic LLM Middleman conversation entity (LLMM-005).

These drive a **fake adapter** whose ``stream_turn`` yields scripted deltas or raises
scripted exceptions — no real backend is touched (that is the adapter tickets' job).
The focus is the entity's contract: the never-hangs guard, timeout helper,
continue_conversation override, and memory-scope key derivation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_PROMPT, MATCH_ALL
from homeassistant.core import Context, HomeAssistant

from custom_components.llm_middleman.backends.base import (
    BackendAdapter,
    DeltaStream,
    TurnContext,
    build_client_timeout,
)
from custom_components.llm_middleman.const import (
    CONF_MEMORY_SCOPE,
    CONF_TIMEOUT,
    DEFAULT_TIMEOUT,
    DOMAIN,
    ERROR_MESSAGE,
    IDLE_TIMEOUT,
    MEMORY_SCOPE_AGENT,
    MEMORY_SCOPE_CONVERSATION,
    MEMORY_SCOPE_DEVICE,
)
from custom_components.llm_middleman.conversation import LLMMiddlemanConversationEntity

from .conftest import MockChatLog


class FakeAdapter(BackendAdapter):
    """Adapter test double: scripted deltas / exception, records the TurnContext."""

    backend_type = "fake"

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        connection_data: Mapping[str, Any],
    ) -> None:
        super().__init__(hass, session, connection_data)
        self.deltas: list[conversation.AssistantContentDeltaDict] = []
        self.exc: Exception | None = None
        self.raise_after: int = 0
        self.set_continue: bool = False
        self.received_ctx: TurnContext | None = None

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        return None

    async def stream_turn(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        ctx: TurnContext,
    ) -> DeltaStream:
        self.received_ctx = ctx
        if self.set_continue:
            ctx.continue_conversation = True
        for index, delta in enumerate(self.deltas):
            if self.exc is not None and index >= self.raise_after:
                raise self.exc
            yield delta
        if self.exc is not None and self.raise_after >= len(self.deltas):
            raise self.exc


def _make_adapter(hass: HomeAssistant, **script: Any) -> FakeAdapter:
    adapter = FakeAdapter(hass, MagicMock(), {})
    for key, value in script.items():
        setattr(adapter, key, value)
    return adapter


def _make_entity(
    hass: HomeAssistant,
    adapter: FakeAdapter,
    *,
    subentry_data: Mapping[str, Any] | None = None,
    subentry_id: str = "sub-1",
) -> LLMMiddlemanConversationEntity:
    entry = MagicMock()
    entry.runtime_data = adapter
    subentry = MagicMock(spec=ConfigSubentry)
    subentry.subentry_id = subentry_id
    subentry.title = "Agent"
    subentry.data = dict(subentry_data) if subentry_data is not None else {}
    entity = LLMMiddlemanConversationEntity(entry, subentry)
    entity.entity_id = "conversation.test"
    entity.hass = hass
    return entity


def _user_input(
    *, device_id: str | None = None, extra_system_prompt: str | None = None
) -> conversation.ConversationInput:
    return conversation.ConversationInput(
        text="hello",
        context=Context(),
        conversation_id="mock-conversation-id",
        device_id=device_id,
        satellite_id=None,
        language="en",
        agent_id="conversation.test",
        extra_system_prompt=extra_system_prompt,
    )


async def _collect(stream: DeltaStream) -> list[dict[str, Any]]:
    return [dict(delta) async for delta in stream]


# --- static attributes -------------------------------------------------------


def test_static_attributes(hass: HomeAssistant) -> None:
    """Streaming on, all languages, unique_id + device_info key on the subentry."""
    entity = _make_entity(hass, _make_adapter(hass), subentry_id="sub-xyz")
    assert entity.supports_streaming is True
    assert entity.supported_languages == MATCH_ALL
    assert entity.unique_id == "sub-xyz"
    assert entity.device_info is not None
    assert (DOMAIN, "sub-xyz") in entity.device_info["identifiers"]  # pyright: ignore[reportTypedDictNotRequiredAccess]


# --- happy path --------------------------------------------------------------


async def test_streams_deltas_and_builds_result(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """Scripted deltas stream into the chat log; result text == concatenation."""
    adapter = _make_adapter(
        hass,
        deltas=[{"role": "assistant"}, {"content": "Hello "}, {"content": "world"}],
    )
    entity = _make_entity(hass, adapter)

    result = await entity._async_handle_message(_user_input(), mock_chat_log)

    last = mock_chat_log.content[-1]
    assert isinstance(last, conversation.AssistantContent)
    assert last.content == "Hello world"
    assert result.response.speech["plain"]["speech"] == "Hello world"


# --- never-hangs guard -------------------------------------------------------


async def test_guard_silent_end(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """A stream that yields nothing still produces one fallback assistant message."""
    entity = _make_entity(hass, _make_adapter(hass, deltas=[]))

    await entity._async_run_turn(_user_input(), mock_chat_log)

    last = mock_chat_log.content[-1]
    assert isinstance(last, conversation.AssistantContent)
    assert last.content == ERROR_MESSAGE


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("chunk too big"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        TimeoutError(),
    ],
)
async def test_guard_exception_before_content(hass: HomeAssistant, mock_chat_log: MockChatLog, exc: Exception) -> None:
    """An exception before any delta yields the fallback and logs; no raise."""
    entity = _make_entity(hass, _make_adapter(hass, deltas=[], exc=exc, raise_after=0))

    with patch("custom_components.llm_middleman.conversation._LOGGER") as logger:
        await entity._async_run_turn(_user_input(), mock_chat_log)

    last = mock_chat_log.content[-1]
    assert isinstance(last, conversation.AssistantContent)
    assert last.content == ERROR_MESSAGE
    logger.exception.assert_called_once()


async def test_guard_exception_after_content(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """Partial content survives AND a fallback message is appended; no raise."""
    adapter = _make_adapter(
        hass,
        deltas=[{"role": "assistant"}, {"content": "partial"}],
        exc=RuntimeError("boom"),
        raise_after=2,
    )
    entity = _make_entity(hass, adapter)

    with patch("custom_components.llm_middleman.conversation._LOGGER") as logger:
        await entity._async_run_turn(_user_input(), mock_chat_log)

    last = mock_chat_log.content[-1]
    assert isinstance(last, conversation.AssistantContent)
    assert last.content is not None
    assert "partial" in last.content
    assert ERROR_MESSAGE in last.content
    logger.exception.assert_called_once()


async def test_guard_injects_role_first(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """A first delta lacking a role gets a leading {'role': 'assistant'} injected."""
    adapter = _make_adapter(hass, deltas=[{"content": "x"}])
    entity = _make_entity(hass, adapter)
    ctx = TurnContext(options={}, memory_key="k")

    collected = await _collect(entity._guarded(adapter.stream_turn(mock_chat_log, _user_input(), ctx)))

    assert collected[0] == {"role": "assistant"}
    assert collected[1] == {"content": "x"}


async def test_empty_delta_passes_untrimmed(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """An empty-string content delta is not dropped by the guard."""
    adapter = _make_adapter(hass, deltas=[{"role": "assistant"}, {"content": ""}, {"content": "a"}])
    entity = _make_entity(hass, adapter)
    ctx = TurnContext(options={}, memory_key="k")

    collected = await _collect(entity._guarded(adapter.stream_turn(mock_chat_log, _user_input(), ctx)))

    assert {"content": ""} in collected
    assert {"content": "a"} in collected


# --- timeouts ----------------------------------------------------------------


def test_build_client_timeout_defaults() -> None:
    """No option → total=DEFAULT_TIMEOUT, sock_read=IDLE_TIMEOUT."""
    timeout = build_client_timeout({})
    assert isinstance(timeout, aiohttp.ClientTimeout)
    assert timeout.total == DEFAULT_TIMEOUT
    assert timeout.sock_read == IDLE_TIMEOUT


def test_build_client_timeout_per_agent() -> None:
    """The per-agent CONF_TIMEOUT overrides the total; sock_read stays idle."""
    timeout = build_client_timeout({CONF_TIMEOUT: 120})
    assert timeout.total == 120
    assert timeout.sock_read == IDLE_TIMEOUT


# --- follow-up listening -----------------------------------------------------


async def test_continue_conversation_override(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """A non-'?' reply + adapter-set ctx.continue_conversation → result flag True."""
    adapter = _make_adapter(
        hass,
        deltas=[{"role": "assistant"}, {"content": "Sure thing."}],
        set_continue=True,
    )
    entity = _make_entity(hass, adapter)

    result = await entity._async_handle_message(_user_input(), mock_chat_log)

    # HA's own '?'-detection is False (reply ends with '.'), so True comes from the override.
    assert mock_chat_log.continue_conversation is False
    assert result.continue_conversation is True


async def test_continue_conversation_default_false(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """No override and a non-'?' reply → follow-up stays off."""
    adapter = _make_adapter(hass, deltas=[{"role": "assistant"}, {"content": "Done."}])
    entity = _make_entity(hass, adapter)

    result = await entity._async_handle_message(_user_input(), mock_chat_log)

    assert result.continue_conversation is False


# --- memory scope ------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "device_id", "expected"),
    [
        (MEMORY_SCOPE_CONVERSATION, "dev-9", "mock-conversation-id"),
        (None, None, "mock-conversation-id"),  # absent option defaults to conversation
        (MEMORY_SCOPE_DEVICE, "dev-9", "dev-9"),
        (MEMORY_SCOPE_DEVICE, None, "mock-conversation-id"),  # no device → fall back
        (MEMORY_SCOPE_AGENT, "dev-9", "sub-1"),
    ],
)
async def test_memory_key_scopes(
    hass: HomeAssistant,
    mock_chat_log: MockChatLog,
    scope: str | None,
    device_id: str | None,
    expected: str,
) -> None:
    """The derived key the adapter receives matches the configured memory scope."""
    subentry_data: dict[str, Any] = {} if scope is None else {CONF_MEMORY_SCOPE: scope}
    adapter = _make_adapter(hass, deltas=[{"role": "assistant"}, {"content": "ok"}])
    entity = _make_entity(hass, adapter, subentry_data=subentry_data)

    await entity._async_run_turn(_user_input(device_id=device_id), mock_chat_log)

    assert adapter.received_ctx is not None
    assert adapter.received_ctx.memory_key == expected


# --- provide_llm_data wiring -------------------------------------------------


async def test_provides_llm_data_without_ha_tools(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    """llm_api is None (LLMM-014 wires tools); the subentry prompt is forwarded."""
    adapter = _make_adapter(hass, deltas=[{"role": "assistant"}, {"content": "ok"}])
    entity = _make_entity(hass, adapter, subentry_data={CONF_PROMPT: "You are a test agent."})

    with patch.object(mock_chat_log, "async_provide_llm_data", wraps=mock_chat_log.async_provide_llm_data) as spy:
        await entity._async_handle_message(_user_input(extra_system_prompt="be brief"), mock_chat_log)

    args = spy.call_args[0]
    assert args[1] is None  # user_llm_hass_api — LLMM-014 wires tools
    assert args[2] == "You are a test agent."  # user_llm_prompt (subentry CONF_PROMPT)
    assert args[3] == "be brief"  # user_extra_system_prompt (start_conversation support)
    assert mock_chat_log.llm_api is None


async def test_converse_error_is_returned(hass: HomeAssistant) -> None:
    """A pre-flight ConverseError is surfaced as a result, not raised."""
    adapter = _make_adapter(hass, deltas=[{"role": "assistant"}, {"content": "unused"}])
    entity = _make_entity(hass, adapter)

    user_input = MagicMock(spec=conversation.ConversationInput)
    user_input.extra_system_prompt = None
    user_input.as_llm_context.return_value = MagicMock()

    chat_log = MagicMock(spec=conversation.ChatLog)
    chat_log.async_provide_llm_data = AsyncMock(side_effect=conversation.ConverseError("boom", "conv-1", MagicMock()))

    # ConverseError before any streaming; the adapter must not be driven.
    result = await entity._async_handle_message(user_input, chat_log)

    assert isinstance(result, conversation.ConversationResult)
    assert adapter.received_ctx is None
