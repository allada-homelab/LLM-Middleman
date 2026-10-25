"""Tests for the backend adapter interface and factory (LLMM-003).

Top-level path (not ``tests/backends/``) to avoid depending on LLMM-004's test
package layout.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Mapping
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
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
    BackendStatusError,
    BackendStreamError,
    TurnContext,
    build_client_timeout,
    probe_json,
)
from custom_components.llm_middleman.const import CONF_TIMEOUT, DEFAULT_TIMEOUT, N8N_DEFAULT_TIMEOUT
from tests.conftest import (
    FakeJsonResponse,
    collect,
    make_input,
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
    deltas = await collect(adapter.stream_turn(mock_chat_log, make_input(), ctx))
    assert deltas == [{"role": "assistant"}, {"content": "x"}]


async def test_isinstance_aiohttp_session_contract(hass: HomeAssistant) -> None:
    # The constructor stores the shared session unchanged.
    session = async_get_clientsession(hass)
    adapter = _DummyAdapter(hass, session, {})
    assert isinstance(adapter.session, aiohttp.ClientSession)
    assert adapter.session is session


def test_build_client_timeout_has_no_total_deadline() -> None:
    """A live stream must not be killed by a whole-request deadline (the v0 60 s bug)."""
    timeout = build_client_timeout({CONF_TIMEOUT: 7})
    assert timeout.total is None
    assert timeout.sock_connect == 7
    # The agent Timeout is also how long the backend may stay silent (regression since 1.1.4).
    assert timeout.sock_read == 7


def test_build_client_timeout_default() -> None:
    timeout = build_client_timeout({})
    assert timeout.total is None
    assert timeout.sock_connect == DEFAULT_TIMEOUT
    assert timeout.sock_read == DEFAULT_TIMEOUT


def test_build_client_timeout_preset_default() -> None:
    # n8n's own default applies to both bounds when the agent leaves Timeout unset.
    timeout = build_client_timeout({}, N8N_DEFAULT_TIMEOUT)
    assert timeout.total is None
    assert timeout.sock_connect == N8N_DEFAULT_TIMEOUT
    assert timeout.sock_read == N8N_DEFAULT_TIMEOUT


def _probe_session(response: FakeJsonResponse) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(return_value=response)
    session.post = MagicMock(return_value=response)
    return session


async def test_probe_json_returns_object_body() -> None:
    session = _probe_session(FakeJsonResponse({"models": []}))
    assert await probe_json(session, "http://x/api", label="X", headers={}) == {"models": []}
    assert session.get.call_args.kwargs["timeout"].total == DEFAULT_TIMEOUT


@pytest.mark.parametrize("payload", [[{"model": "m"}], "ok", 3, None])
async def test_probe_json_non_object_body_is_connection_error(payload: object) -> None:
    """A non-object JSON body (e.g. a bare list) is a connection error, never a crash."""
    session = _probe_session(FakeJsonResponse(payload))
    with pytest.raises(BackendConnectionError, match="non-object") as excinfo:
        await probe_json(session, "http://x/api", label="X", headers={})
    assert not isinstance(excinfo.value, BackendAuthError)


async def test_probe_json_invalid_json_is_connection_error() -> None:
    session = _probe_session(FakeJsonResponse(json.JSONDecodeError("Expecting value", "<html>", 0)))
    with pytest.raises(BackendConnectionError, match="invalid JSON"):
        await probe_json(session, "http://x/api", label="X", headers={})


@pytest.mark.parametrize(
    ("status", "exc"), [(401, BackendAuthError), (403, BackendAuthError), (500, BackendStatusError)]
)
async def test_probe_json_maps_error_status(status: int, exc: type[Exception]) -> None:
    session = _probe_session(FakeJsonResponse(status=status))
    with pytest.raises(exc, match=f"HTTP {status}"):
        await probe_json(session, "http://x/api", label="X", headers={})


async def test_probe_json_transport_error_is_connection_error() -> None:
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))
    with pytest.raises(BackendConnectionError, match="Cannot reach X") as excinfo:
        await probe_json(session, "http://x/api", label="X", headers={})
    assert not isinstance(excinfo.value, BackendStatusError)


async def test_probe_json_unparsed_post_checks_status_only() -> None:
    """``parse=False`` (a list-bodied endpoint) checks the status and returns ``{}``."""
    session = _probe_session(FakeJsonResponse(["assistant"]))
    result = await probe_json(session, "http://x/search", label="X", headers={}, method="POST", data="{}", parse=False)
    assert result == {}
    assert session.post.call_args.kwargs["data"] == "{}"
    session.get.assert_not_called()
