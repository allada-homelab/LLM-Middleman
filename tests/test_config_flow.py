"""Tests for the LLM Middleman parent config flow (LLMM-006).

The backend registry (``BACKEND_TO_CLS``) is empty until adapter tickets land, so
these tests patch it with fake adapter classes whose ``async_validate_connection``
succeeds or raises the typed connection errors the flow maps.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_middleman.backends.base import (
    BackendAuthError,
    BackendConnectionError,
)
from custom_components.llm_middleman.const import (
    BACKEND_N8N,
    BACKEND_OPENAI_COMPAT,
    CONF_API_KEY,
    CONF_BACKEND_TYPE,
    CONF_BASE_URL,
    CONF_WEBHOOK_URL,
    DOMAIN,
)

_REGISTRY_PATH = "custom_components.llm_middleman.config_flow.BACKEND_TO_CLS"


def _fake_adapter(exc: Exception | None = None) -> type:
    """Build a fake adapter class whose probe raises ``exc`` (or succeeds)."""

    class _FakeAdapter:
        @classmethod
        async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
            if exc is not None:
                raise exc

    return _FakeAdapter


def _registry(**adapters: type) -> dict[str, type]:
    """Registry keyed by backend type. Defaults to a succeeding openai_compat probe."""
    return adapters or {BACKEND_OPENAI_COMPAT: _fake_adapter()}


# HA's flow manager is only partially typed and its result is a TypedDict whose keys
# are all NotRequired; these thin wrappers localize the one unavoidable suppression
# and hand back a plain dict so the assertions stay clean.
async def _init(hass: HomeAssistant) -> dict[str, Any]:
    """Start the user flow, returning the result as a plain dict."""
    result = await hass.config_entries.flow.async_init(  # pyright: ignore[reportUnknownMemberType]
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return dict(result)


async def _configure(hass: HomeAssistant, flow_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
    """Advance a flow, returning the result as a plain dict."""
    result = await hass.config_entries.flow.async_configure(flow_id, user_input)  # pyright: ignore[reportUnknownMemberType]
    return dict(result)


async def _init_backend_step(hass: HomeAssistant, backend_type: str) -> dict[str, Any]:
    """Run step 1 (pick ``backend_type``) and return the connection-step form result."""
    result = await _init(hass)
    return await _configure(hass, result["flow_id"], {CONF_BACKEND_TYPE: backend_type})


async def test_user_step_lists_backends(hass: HomeAssistant) -> None:
    """Step 1 is a form exposing the backend-type selector over the registry keys."""
    registry = {BACKEND_OPENAI_COMPAT: _fake_adapter(), BACKEND_N8N: _fake_adapter()}
    with patch(_REGISTRY_PATH, registry):
        result = await _init(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert CONF_BACKEND_TYPE in {str(marker) for marker in result["data_schema"].schema}


async def test_full_flow_openai_creates_entry(hass: HomeAssistant) -> None:
    """Picking openai and passing the probe creates an entry with the type + stripped URL."""
    with patch(_REGISTRY_PATH, _registry()):
        form = await _init_backend_step(hass, BACKEND_OPENAI_COMPAT)
        assert form["type"] is FlowResultType.FORM
        assert form["step_id"] == BACKEND_OPENAI_COMPAT

        result = await _configure(
            hass,
            form["flow_id"],
            {CONF_BASE_URL: "http://backend.local:8000/v1/", CONF_API_KEY: "secret"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BACKEND_TYPE] == BACKEND_OPENAI_COMPAT
    assert result["data"][CONF_BASE_URL] == "http://backend.local:8000/v1"
    assert result["data"][CONF_API_KEY] == "secret"


async def test_probe_connection_failure_shows_cannot_connect(hass: HomeAssistant) -> None:
    """A BackendConnectionError re-shows the form with cannot_connect and no entry."""
    registry = {BACKEND_OPENAI_COMPAT: _fake_adapter(BackendConnectionError("down"))}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_backend_step(hass, BACKEND_OPENAI_COMPAT)
        result = await _configure(hass, form["flow_id"], {CONF_BASE_URL: "http://backend.local:8000"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_probe_auth_failure_shows_invalid_auth(hass: HomeAssistant) -> None:
    """A BackendAuthError maps to invalid_auth (caught before the connection base class)."""
    registry = {BACKEND_OPENAI_COMPAT: _fake_adapter(BackendAuthError("nope"))}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_backend_step(hass, BACKEND_OPENAI_COMPAT)
        result = await _configure(hass, form["flow_id"], {CONF_BASE_URL: "http://backend.local:8000"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_no_duplicate_abort(hass: HomeAssistant) -> None:
    """Two entries with the same base URL are both created (no unique_id, no abort)."""
    with patch(_REGISTRY_PATH, _registry()):
        for _ in range(2):
            form = await _init_backend_step(hass, BACKEND_OPENAI_COMPAT)
            result = await _configure(hass, form["flow_id"], {CONF_BASE_URL: "http://backend.local:8000"})
            assert result["type"] is FlowResultType.CREATE_ENTRY

    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_n8n_webhook_not_normalized(hass: HomeAssistant) -> None:
    """The opaque n8n webhook URL is stored verbatim, trailing slash intact."""
    registry = {BACKEND_N8N: _fake_adapter()}
    webhook = "https://n8n.local/webhook/abc/chat/"
    with patch(_REGISTRY_PATH, registry):
        form = await _init_backend_step(hass, BACKEND_N8N)
        result = await _configure(
            hass,
            form["flow_id"],
            {CONF_WEBHOOK_URL: webhook, "target_type": "chat_trigger", "auth_type": "none"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_WEBHOOK_URL] == webhook


async def test_reconfigure_updates_connection(hass: HomeAssistant) -> None:
    """Reconfigure re-runs the fixed backend's step and updates the entry data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="OpenAI-compatible",
        data={CONF_BACKEND_TYPE: BACKEND_OPENAI_COMPAT, CONF_BASE_URL: "http://old.local:8000"},
    )
    entry.add_to_hass(hass)

    with patch(_REGISTRY_PATH, _registry()):
        form: dict[str, Any] = dict(await entry.start_reconfigure_flow(hass))
        assert form["step_id"] == BACKEND_OPENAI_COMPAT
        result = await _configure(hass, form["flow_id"], {CONF_BASE_URL: "http://new.local:9000"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_BACKEND_TYPE] == BACKEND_OPENAI_COMPAT
    assert entry.data[CONF_BASE_URL] == "http://new.local:9000"
