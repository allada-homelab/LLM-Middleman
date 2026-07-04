"""Tests for the backend adapter interface and factory (LLMM-003).

Top-level path (not ``tests/backends/``) to avoid depending on LLMM-004's test
package layout.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from typing import Any

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.llm_middleman.backends import (
    BACKEND_TO_CLS,
    BackendAdapter,
    get_backend_cls,
)
from custom_components.llm_middleman.backends.base import (
    BackendAuthError,
    BackendConnectionError,
    BackendStreamError,
    TurnContext,
)


class _DummyAdapter(BackendAdapter):
    """Minimal concrete adapter for exercising the ABC."""

    backend_type = "dummy"

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        return None

    async def stream_turn(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        ctx: TurnContext,
    ) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        yield {"role": "assistant"}
        yield {"content": "x"}


class _MissingStreamAdapter(BackendAdapter):
    """Adapter missing the abstract ``stream_turn`` — must stay non-instantiable."""

    backend_type = "missing"

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        return None


def _make_input() -> conversation.ConversationInput:
    return conversation.ConversationInput(
        text="hi",
        context=Context(),
        conversation_id="conv-1",
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="agent-1",
    )


async def test_concrete_adapter_instantiates(hass: HomeAssistant) -> None:
    adapter = _DummyAdapter(hass, async_get_clientsession(hass), {"base_url": "x"})
    assert isinstance(adapter, BackendAdapter)
    assert adapter.hass is hass
    assert adapter.connection_data == {"base_url": "x"}


async def test_missing_abstract_raises_typeerror(hass: HomeAssistant) -> None:
    session = async_get_clientsession(hass)
    with pytest.raises(TypeError):
        _MissingStreamAdapter(hass, session, {})  # type: ignore[abstract]


def test_classvars() -> None:
    assert _DummyAdapter.backend_type == "dummy"
    assert _DummyAdapter.supports_ha_tools is False
    assert _DummyAdapter.supports_memory_scope is False


async def test_async_list_models_default(hass: HomeAssistant) -> None:
    assert await _DummyAdapter.async_list_models(hass, {}) is None


def test_turn_context_defaults_and_mutation_isolation() -> None:
    ctx = TurnContext(options={}, memory_key="k")
    assert ctx.continue_conversation is False

    other = TurnContext(options={}, memory_key="k2")
    ctx.continue_conversation = True
    assert ctx.continue_conversation is True
    assert other.continue_conversation is False


def test_factory_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(BACKEND_TO_CLS, "dummy", _DummyAdapter)
    assert get_backend_cls("dummy") is _DummyAdapter
    with pytest.raises(ValueError, match="Unknown backend type"):
        get_backend_cls("nope")


def test_factory_registers_adapters_and_unknown_raises() -> None:
    # Adapters register as their tickets land; unknown types still raise.
    for backend in ("openai_compat", "converse", "ollama", "langgraph", "n8n"):
        assert backend in BACKEND_TO_CLS
    with pytest.raises(ValueError, match="Unknown backend type"):
        get_backend_cls("nonexistent_backend")


def test_exception_surface() -> None:
    assert issubclass(BackendAuthError, BackendConnectionError)
    assert issubclass(BackendConnectionError, HomeAssistantError)
    assert issubclass(BackendStreamError, Exception)


async def test_stream_turn_yields_delta_dicts(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    adapter = _DummyAdapter(hass, async_get_clientsession(hass), {})
    ctx = TurnContext(options={}, memory_key="k")
    deltas = [delta async for delta in adapter.stream_turn(mock_chat_log, _make_input(), ctx)]
    assert deltas == [{"role": "assistant"}, {"content": "x"}]


async def test_isinstance_aiohttp_session_contract(hass: HomeAssistant) -> None:
    # The constructor stores the shared session unchanged.
    session = async_get_clientsession(hass)
    adapter = _DummyAdapter(hass, session, {})
    assert isinstance(adapter.session, aiohttp.ClientSession)
    assert adapter.session is session
