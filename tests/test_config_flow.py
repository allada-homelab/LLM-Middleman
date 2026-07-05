"""Tests for the LLM Middleman parent config flow (LLMM-006).

The backend registry (``BACKEND_TO_CLS``) is empty until adapter tickets land, so
these tests patch it with fake adapter classes whose ``async_validate_connection``
succeeds or raises the typed connection errors the flow maps.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_LLM_HASS_API, CONF_NAME, CONF_PROMPT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import (
    SelectSelector,  # pyright: ignore[reportUnknownVariableType]
    TextSelector,  # pyright: ignore[reportUnknownVariableType]
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_middleman.backends.base import (
    BackendAdapter,
    BackendAuthError,
    BackendConnectionError,
)
from custom_components.llm_middleman.config_flow import (
    ConversationSubentryFlowHandler,
    LLMMiddlemanConfigFlow,
)
from custom_components.llm_middleman.const import (
    BACKEND_CONVERSE,
    BACKEND_N8N,
    BACKEND_OPENAI_COMPAT,
    CONF_API_KEY,
    CONF_BACKEND_TYPE,
    CONF_BASE_URL,
    CONF_MAX_HISTORY,
    CONF_MEMORY_SCOPE,
    CONF_MODEL,
    CONF_TIMEOUT,
    CONF_WEBHOOK_URL,
    DOMAIN,
    SUBENTRY_TYPE_CONVERSATION,
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
    """Picking openai and passing the probe creates an entry with the type + root URL.

    The adapter hardcodes the ``/v1`` prefix, so a submitted ``…/v1/`` is normalized
    down to the server root (trailing slash *and* one trailing ``/v1`` stripped).
    """
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
    assert result["data"][CONF_BASE_URL] == "http://backend.local:8000"
    assert result["data"][CONF_API_KEY] == "secret"


@pytest.mark.parametrize(
    "submitted",
    [
        "http://backend.local:8000/v1",
        "http://backend.local:8000/v1/",
    ],
)
async def test_openai_compat_strips_v1_suffix(hass: HomeAssistant, submitted: str) -> None:
    """A base_url entered with a trailing /v1 (or /v1/) is stored as the server root.

    The openai_compat adapter appends ``/v1/...`` itself, so the conventional
    OpenAI-style ``…/v1`` base URL must be normalized to the root or requests
    double-path to ``/v1/v1/...`` and 404. This proves the stored entry data is the
    root, from which the adapter's probe URL (``{root}/v1/models``) is well-formed.
    """
    with patch(_REGISTRY_PATH, _registry()):
        form = await _init_backend_step(hass, BACKEND_OPENAI_COMPAT)
        result = await _configure(hass, form["flow_id"], {CONF_BASE_URL: submitted})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BASE_URL] == "http://backend.local:8000"


async def test_openai_compat_root_unchanged(hass: HomeAssistant) -> None:
    """A clean server root (no /v1) is stored verbatim — only a real /v1 suffix is stripped."""
    with patch(_REGISTRY_PATH, _registry()):
        form = await _init_backend_step(hass, BACKEND_OPENAI_COMPAT)
        result = await _configure(hass, form["flow_id"], {CONF_BASE_URL: "http://backend.local:8000"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BASE_URL] == "http://backend.local:8000"


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


# --- Conversation subentry flow (LLMM-007) ------------------------------------


def _sub_adapter(
    *,
    backend_type: str,
    models: list[str] | None = None,
    models_raise: bool = False,
    supports_memory_scope: bool = False,
    supports_ha_tools: bool = False,
) -> type[BackendAdapter]:
    """Fake adapter exposing the capability ClassVars + model probe the subentry uses.

    Subclasses the real ABC (never instantiated) so the capability ClassVars and the
    ``async_list_models`` override are the genuine typed surface the flow reads.
    """
    # Bind params to distinctly named locals so the class body below doesn't shadow them.
    bt, ms, ht = backend_type, supports_memory_scope, supports_ha_tools
    model_list, raise_probe = models, models_raise

    class _SubAdapter(BackendAdapter):
        backend_type = bt
        supports_memory_scope = ms
        supports_ha_tools = ht

        @classmethod
        async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
            return None

        @classmethod
        async def async_list_models(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> list[str] | None:
            if raise_probe:
                raise BackendConnectionError("probe down")
            return model_list

        def stream_turn(self, chat_log: Any, user_input: Any, ctx: Any) -> Any:
            raise NotImplementedError

    return _SubAdapter


async def _init_subentry(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Start the add-conversation-agent (user) subentry flow, returning a plain dict."""
    result = await hass.config_entries.subentries.async_init(  # pyright: ignore[reportUnknownMemberType]
        (entry.entry_id, SUBENTRY_TYPE_CONVERSATION),
        context={"source": config_entries.SOURCE_USER},
    )
    return dict(result)


async def _configure_subentry(hass: HomeAssistant, flow_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
    """Advance a subentry flow, returning the result as a plain dict."""
    result = await hass.config_entries.subentries.async_configure(flow_id, user_input)  # pyright: ignore[reportUnknownMemberType]
    return dict(result)


def _parent_entry(hass: HomeAssistant, backend_type: str, **kwargs: Any) -> MockConfigEntry:
    """Add a configured parent backend entry to hass."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={CONF_BACKEND_TYPE: backend_type}, **kwargs)
    entry.add_to_hass(hass)
    return entry


def _schema_keys(schema: vol.Schema) -> set[str]:
    """Field keys of a rendered vol.Schema."""
    return {str(marker.schema) for marker in schema.schema}


def _selector_for(schema: vol.Schema, key: str) -> Any:
    """Return the selector object bound to ``key`` in a rendered schema."""
    for marker, selector in schema.schema.items():
        if str(marker.schema) == key:
            return selector
    raise AssertionError(f"{key} not in schema")


def _suggested_values(schema: vol.Schema) -> dict[str, Any]:
    """Map each field key to its ``description['suggested_value']`` (if any)."""
    out: dict[str, Any] = {}
    for marker in schema.schema:
        desc = getattr(marker, "description", None)
        if desc and "suggested_value" in desc:
            out[str(marker.schema)] = desc["suggested_value"]
    return out


def test_supported_subentry_types() -> None:
    """The parent flow advertises the conversation subentry type."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={CONF_BACKEND_TYPE: BACKEND_OPENAI_COMPAT})
    types = LLMMiddlemanConfigFlow.async_get_supported_subentry_types(entry)  # type: ignore[arg-type]
    assert types[SUBENTRY_TYPE_CONVERSATION] is ConversationSubentryFlowHandler


async def test_create_conversation_subentry(hass: HomeAssistant) -> None:
    """The user step submits name/prompt/timeout and creates a subentry carrying them."""
    entry = _parent_entry(hass, BACKEND_CONVERSE)
    registry = {BACKEND_CONVERSE: _sub_adapter(backend_type=BACKEND_CONVERSE)}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_subentry(hass, entry)
        assert form["type"] is FlowResultType.FORM
        assert form["step_id"] == "set_options"
        keys = _schema_keys(form["data_schema"])
        assert {CONF_NAME, CONF_PROMPT, CONF_MAX_HISTORY, CONF_TIMEOUT} <= keys

        result = await _configure_subentry(
            hass,
            form["flow_id"],
            {CONF_NAME: "Kitchen agent", CONF_PROMPT: "Be brief.", CONF_TIMEOUT: 45},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen agent"
    assert result["data"][CONF_PROMPT] == "Be brief."
    assert result["data"][CONF_TIMEOUT] == 45
    # The name is the subentry title, not stored option data.
    assert CONF_NAME not in result["data"]


async def test_model_dropdown_openai(hass: HomeAssistant) -> None:
    """A reachable model catalog yields a dropdown-with-free-text; a failed probe → text."""
    entry = _parent_entry(hass, BACKEND_OPENAI_COMPAT)

    registry = {BACKEND_OPENAI_COMPAT: _sub_adapter(backend_type=BACKEND_OPENAI_COMPAT, models=["m1", "m2"])}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_subentry(hass, entry)
    selector = _selector_for(form["data_schema"], CONF_MODEL)
    assert isinstance(selector, SelectSelector)
    assert selector.config["options"] == ["m1", "m2"]  # pyright: ignore[reportUnknownMemberType]
    assert selector.config["custom_value"] is True  # pyright: ignore[reportUnknownMemberType]

    registry = {BACKEND_OPENAI_COMPAT: _sub_adapter(backend_type=BACKEND_OPENAI_COMPAT, models_raise=True)}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_subentry(hass, entry)
    assert isinstance(_selector_for(form["data_schema"], CONF_MODEL), TextSelector)


async def test_model_field_absent_without_catalog(hass: HomeAssistant) -> None:
    """A catalog-less backend (async_list_models → None) renders no model field."""
    entry = _parent_entry(hass, BACKEND_CONVERSE)
    registry = {BACKEND_CONVERSE: _sub_adapter(backend_type=BACKEND_CONVERSE)}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_subentry(hass, entry)
    assert CONF_MODEL not in _schema_keys(form["data_schema"])


async def test_memory_scope_gated(hass: HomeAssistant) -> None:
    """CONF_MEMORY_SCOPE appears only for backends that support it."""
    entry = _parent_entry(hass, BACKEND_OPENAI_COMPAT)

    stateless = {BACKEND_OPENAI_COMPAT: _sub_adapter(backend_type=BACKEND_OPENAI_COMPAT)}
    with patch(_REGISTRY_PATH, stateless):
        form = await _init_subentry(hass, entry)
    assert CONF_MEMORY_SCOPE not in _schema_keys(form["data_schema"])

    stateful = {BACKEND_OPENAI_COMPAT: _sub_adapter(backend_type=BACKEND_OPENAI_COMPAT, supports_memory_scope=True)}
    with patch(_REGISTRY_PATH, stateful):
        form = await _init_subentry(hass, entry)
    assert CONF_MEMORY_SCOPE in _schema_keys(form["data_schema"])


async def test_llm_hass_api_gated(hass: HomeAssistant) -> None:
    """CONF_LLM_HASS_API appears only for tool-capable backends, as a multi-select."""
    entry = _parent_entry(hass, BACKEND_OPENAI_COMPAT)

    text_only = {BACKEND_OPENAI_COMPAT: _sub_adapter(backend_type=BACKEND_OPENAI_COMPAT, supports_ha_tools=False)}
    with patch(_REGISTRY_PATH, text_only):
        form = await _init_subentry(hass, entry)
    assert CONF_LLM_HASS_API not in _schema_keys(form["data_schema"])

    tool_capable = {BACKEND_OPENAI_COMPAT: _sub_adapter(backend_type=BACKEND_OPENAI_COMPAT, supports_ha_tools=True)}
    with patch(_REGISTRY_PATH, tool_capable):
        form = await _init_subentry(hass, entry)
    assert CONF_LLM_HASS_API in _schema_keys(form["data_schema"])
    selector = _selector_for(form["data_schema"], CONF_LLM_HASS_API)
    assert isinstance(selector, SelectSelector)
    assert selector.config["multiple"] is True  # pyright: ignore[reportUnknownMemberType]


async def test_reconfigure_prefills(hass: HomeAssistant) -> None:
    """Reconfigure prefills the stored option values and updates them on submit."""
    stored = {CONF_PROMPT: "Custom prompt", CONF_MAX_HISTORY: 5, CONF_TIMEOUT: 90}
    entry = _parent_entry(
        hass,
        BACKEND_CONVERSE,
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_CONVERSATION,
                "data": stored,
                "title": "Existing agent",
                "unique_id": None,
            }
        ],
    )
    subentry_id = next(iter(entry.subentries))
    registry = {BACKEND_CONVERSE: _sub_adapter(backend_type=BACKEND_CONVERSE)}

    with patch(_REGISTRY_PATH, registry):
        form: dict[str, Any] = dict(await entry.start_subentry_reconfigure_flow(hass, subentry_id))
        assert form["step_id"] == "set_options"
        # No name field on reconfigure (the title stays put).
        assert CONF_NAME not in _schema_keys(form["data_schema"])
        suggested = _suggested_values(form["data_schema"])
        assert suggested[CONF_PROMPT] == "Custom prompt"
        assert suggested[CONF_MAX_HISTORY] == 5
        assert suggested[CONF_TIMEOUT] == 90

        result = await _configure_subentry(
            hass,
            form["flow_id"],
            {CONF_PROMPT: "Updated prompt", CONF_MAX_HISTORY: 3, CONF_TIMEOUT: 60},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data[CONF_PROMPT] == "Updated prompt"
    assert entry.subentries[subentry_id].data[CONF_MAX_HISTORY] == 3
