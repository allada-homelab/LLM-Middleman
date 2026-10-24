"""Tests for the LLM Middleman parent config flow (LLMM-006).

The backend registry (``BACKEND_TO_CLS``) is empty until adapter tickets land, so
these tests patch it with fake adapter classes whose ``async_validate_connection``
succeeds or raises the typed connection errors the flow maps.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_LLM_HASS_API, CONF_NAME, CONF_PROMPT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import llm
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
from custom_components.llm_middleman.backends.dify import DifyAppError
from custom_components.llm_middleman.backends.langgraph import AssistantNotFoundError
from custom_components.llm_middleman.config_flow import (
    ConversationSubentryFlowHandler,
    LLMMiddlemanConfigFlow,
)
from custom_components.llm_middleman.const import (
    BACKEND_CONVERSE,
    BACKEND_DIFY,
    BACKEND_LANGGRAPH,
    BACKEND_N8N,
    BACKEND_OLLAMA,
    BACKEND_OPENAI_COMPAT,
    CONF_API_KEY,
    CONF_ASSISTANT_ID,
    CONF_AUTH_SCHEME,
    CONF_AUTH_TYPE,
    CONF_BACKEND_TYPE,
    CONF_BASE_URL,
    CONF_DIFY_HA_CONTEXT,
    CONF_DIFY_INPUTS,
    CONF_DIFY_USER_FROM_HA,
    CONF_HEADER_NAME,
    CONF_HEADER_VALUE,
    CONF_INPUT_FIELD,
    CONF_INPUT_MESSAGES_KEY,
    CONF_KEEP_ALIVE,
    CONF_MAX_HISTORY,
    CONF_MAX_TOKENS,
    CONF_MEMORY_SCOPE,
    CONF_MODEL,
    CONF_MULTITASK_STRATEGY,
    CONF_NUM_CTX,
    CONF_OUTPUT_FIELD,
    CONF_PASSWORD,
    CONF_RESPONSE_NODE_FILTER,
    CONF_SESSION_FIELD,
    CONF_STATELESS_RUNS,
    CONF_TEMPERATURE,
    CONF_THINK,
    CONF_TIMEOUT,
    CONF_TOKEN,
    CONF_TOP_P,
    CONF_USERNAME,
    CONF_WEBHOOK_URL,
    DOMAIN,
    N8N_AUTH_BASIC,
    N8N_AUTH_HEADER,
    N8N_AUTH_NONE,
    SUBENTRY_TYPE_CONVERSATION,
)

from .test_conversation import FakeAdapter

_REGISTRY_PATH = "custom_components.llm_middleman.config_flow.BACKEND_TO_CLS"
# The registry async_setup_entry builds the adapter from (patched to a fake for reloads).
_SETUP_REGISTRY_PATH = "custom_components.llm_middleman.backends.BACKEND_TO_CLS"


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


@pytest.mark.parametrize("error_key", ["not_chat_app", "agent_not_published"])
async def test_dify_app_error_shows_specific_error(hass: HomeAssistant, error_key: str) -> None:
    """A Dify key for a workflow app / unpublished Agent gets its own error, not cannot_connect."""
    registry = {BACKEND_DIFY: _fake_adapter(DifyAppError(error_key, "wrong app"))}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_backend_step(hass, BACKEND_DIFY)
        result = await _configure(hass, form["flow_id"], {CONF_BASE_URL: "https://api.dify.ai", CONF_API_KEY: "k"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error_key}


async def test_unexpected_probe_error_shows_unknown(hass: HomeAssistant) -> None:
    """An unexpected probe exception re-shows the form with ``unknown`` instead of crashing the flow."""
    registry = {BACKEND_OPENAI_COMPAT: _fake_adapter(RuntimeError("bug"))}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_backend_step(hass, BACKEND_OPENAI_COMPAT)
        result = await _configure(hass, form["flow_id"], {CONF_BASE_URL: "http://backend.local:8000"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def _create_openai_entry(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, Any]:
    with patch(_REGISTRY_PATH, _registry()):
        form = await _init_backend_step(hass, BACKEND_OPENAI_COMPAT)
        return await _configure(hass, form["flow_id"], user_input)


async def test_identical_connection_aborts_as_duplicate(hass: HomeAssistant) -> None:
    """Adding the exact same connection twice aborts; agents belong in subentries."""
    connection = {CONF_BASE_URL: "http://backend.local:8000", CONF_API_KEY: "k1"}
    await _create_openai_entry(hass, connection)

    result = await _create_openai_entry(hass, connection)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_same_url_with_other_credentials_is_not_a_duplicate(hass: HomeAssistant) -> None:
    """One shared URL can back distinct connections (e.g. Dify Cloud apps differ only by key)."""
    await _create_openai_entry(hass, {CONF_BASE_URL: "http://backend.local:8000", CONF_API_KEY: "k1"})

    result = await _create_openai_entry(hass, {CONF_BASE_URL: "http://backend.local:8000", CONF_API_KEY: "k2"})

    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    "submitted",
    [
        "https://api.dify.ai",
        "https://api.dify.ai/",
        "https://api.dify.ai/v1",
        "https://api.dify.ai/v1/",
    ],
)
async def test_dify_appends_v1_suffix(hass: HomeAssistant, submitted: str) -> None:
    """A Dify base_url is normalized to the /v1 root whether or not /v1 was entered.

    The adapter posts against ``{base}/chat-messages`` under the Dify ``/v1`` convention,
    so both ``https://host`` and ``https://host/v1`` must store the same ``…/v1`` root.
    """
    registry = {BACKEND_DIFY: _fake_adapter()}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_backend_step(hass, BACKEND_DIFY)
        assert form["step_id"] == BACKEND_DIFY
        result = await _configure(hass, form["flow_id"], {CONF_BASE_URL: submitted, CONF_API_KEY: "app-xyz"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BACKEND_TYPE] == BACKEND_DIFY
    assert result["data"][CONF_BASE_URL] == "https://api.dify.ai/v1"
    assert result["data"][CONF_API_KEY] == "app-xyz"


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


async def test_reconfigure_reloads_once_via_update_listener(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A loaded entry reloads exactly once on reconfigure: the update listener owns the reload.

    ``async_update_reload_and_abort`` would schedule a second reload on top of the
    listener's and logs a deprecation that breaks in HA 2026.12.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="OpenAI-compatible",
        data={CONF_BACKEND_TYPE: BACKEND_OPENAI_COMPAT, CONF_BASE_URL: "http://old.local:8000"},
    )
    entry.add_to_hass(hass)

    with (
        patch(_REGISTRY_PATH, _registry()),
        patch.dict(_SETUP_REGISTRY_PATH, {BACKEND_OPENAI_COMPAT: FakeAdapter}),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        with patch.object(hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload) as reload:
            form: dict[str, Any] = dict(await entry.start_reconfigure_flow(hass))
            await _configure(hass, form["flow_id"], {CONF_BASE_URL: "http://new.local:9000"})
            await hass.async_block_till_done()

    assert reload.call_count == 1
    assert "should use it for scheduling a reload" not in caplog.text


class _AuthRejectingAdapter(FakeAdapter):
    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        raise BackendAuthError("bad key")


async def test_reconfigure_reloads_entry_whose_setup_failed(hass: HomeAssistant) -> None:
    """A failed-setup entry has no update listener, so reconfigure reloads it itself."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="OpenAI-compatible",
        data={CONF_BACKEND_TYPE: BACKEND_OPENAI_COMPAT, CONF_BASE_URL: "http://old.local:8000"},
    )
    entry.add_to_hass(hass)
    with patch.dict(_SETUP_REGISTRY_PATH, {BACKEND_OPENAI_COMPAT: _AuthRejectingAdapter}):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR

    with (
        patch(_REGISTRY_PATH, _registry()),
        patch.dict(_SETUP_REGISTRY_PATH, {BACKEND_OPENAI_COMPAT: FakeAdapter}),
    ):
        form: dict[str, Any] = dict(await entry.start_reconfigure_flow(hass))
        result = await _configure(hass, form["flow_id"], {CONF_BASE_URL: "http://new.local:9000"})
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.state is config_entries.ConfigEntryState.LOADED


# --- Reauthentication flow ------------------------------------------------------

_OPENAI_DATA = {CONF_BACKEND_TYPE: BACKEND_OPENAI_COMPAT, CONF_BASE_URL: "http://llm.local", CONF_API_KEY: "old"}


def _reauth_entry(hass: HomeAssistant, data: Mapping[str, Any] = _OPENAI_DATA) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, version=2, title="Connection", data=dict(data))
    entry.add_to_hass(hass)
    return entry


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    return dict(await entry.start_reauth_flow(hass))


async def test_reauth_updates_credentials_and_reloads_failed_entry(hass: HomeAssistant) -> None:
    """An entry whose setup failed on auth (no update listener) is updated and reloaded."""
    entry = _reauth_entry(hass)

    with (
        patch(_REGISTRY_PATH, _registry()),
        patch.dict(_SETUP_REGISTRY_PATH, {BACKEND_OPENAI_COMPAT: FakeAdapter}),
    ):
        form = await _start_reauth(hass, entry)
        result = await _configure(hass, form["flow_id"], {CONF_API_KEY: "new"})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {**_OPENAI_DATA, CONF_API_KEY: "new"}
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_reauth_of_loaded_entry_reloads_once_via_update_listener(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A loaded entry's listener owns the reload; reload+listener together is deprecated."""
    entry = _reauth_entry(hass)

    with (
        patch(_REGISTRY_PATH, _registry()),
        patch.dict(_SETUP_REGISTRY_PATH, {BACKEND_OPENAI_COMPAT: FakeAdapter}),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        with patch.object(hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload) as reload:
            form = await _start_reauth(hass, entry)
            await _configure(hass, form["flow_id"], {CONF_API_KEY: "new"})
            await hass.async_block_till_done()

    assert entry.data[CONF_API_KEY] == "new"
    assert reload.call_count == 1
    assert "should use it for scheduling a reload" not in caplog.text


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (BackendAuthError("bad key"), "invalid_auth"),
        (BackendConnectionError("down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_reauth_failed_probe_keeps_form_and_credentials(hass: HomeAssistant, exc: Exception, error: str) -> None:
    """Rejected new credentials re-show the form with the mapped error; the entry is untouched."""
    entry = _reauth_entry(hass)

    with patch(_REGISTRY_PATH, _registry(**{BACKEND_OPENAI_COMPAT: _fake_adapter(exc)})):
        form = await _start_reauth(hass, entry)
        result = await _configure(hass, form["flow_id"], {CONF_API_KEY: "still-wrong"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}
    assert entry.data[CONF_API_KEY] == "old"


@pytest.mark.parametrize(
    ("data", "fields"),
    [
        ({CONF_BACKEND_TYPE: BACKEND_OPENAI_COMPAT}, {CONF_API_KEY}),
        ({CONF_BACKEND_TYPE: BACKEND_OLLAMA}, {CONF_API_KEY}),
        ({CONF_BACKEND_TYPE: BACKEND_LANGGRAPH}, {CONF_API_KEY}),
        ({CONF_BACKEND_TYPE: BACKEND_DIFY}, {CONF_API_KEY}),
        ({CONF_BACKEND_TYPE: BACKEND_CONVERSE}, {CONF_TOKEN}),
        ({CONF_BACKEND_TYPE: BACKEND_N8N, CONF_AUTH_TYPE: N8N_AUTH_BASIC}, {CONF_USERNAME, CONF_PASSWORD}),
        ({CONF_BACKEND_TYPE: BACKEND_N8N, CONF_AUTH_TYPE: N8N_AUTH_HEADER}, {CONF_HEADER_NAME, CONF_HEADER_VALUE}),
    ],
)
async def test_reauth_asks_only_the_backends_credential_fields(
    hass: HomeAssistant, data: dict[str, Any], fields: set[str]
) -> None:
    """Reauth re-asks just the credential the entry's backend (and n8n auth type) uses."""
    form = await _start_reauth(hass, _reauth_entry(hass, data))

    assert form["step_id"] == "reauth_confirm"
    assert _schema_keys(form["data_schema"]) == fields


@pytest.mark.parametrize("data", [{CONF_AUTH_TYPE: N8N_AUTH_NONE}, {}])
async def test_reauth_aborts_for_a_connection_without_credentials(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """An unauthenticated n8n webhook has nothing to re-enter, so reauth aborts cleanly."""
    form = await _start_reauth(hass, _reauth_entry(hass, {CONF_BACKEND_TYPE: BACKEND_N8N, **data}))

    assert form["type"] is FlowResultType.ABORT
    assert form["reason"] == "reauth_not_supported"


async def test_reauth_with_blank_optional_key_removes_it(hass: HomeAssistant) -> None:
    """Submitting an empty optional key (HA omits the field) drops the stale stored key."""
    entry = _reauth_entry(hass)

    with (
        patch(_REGISTRY_PATH, _registry()),
        patch.dict(_SETUP_REGISTRY_PATH, {BACKEND_OPENAI_COMPAT: FakeAdapter}),
    ):
        form = await _start_reauth(hass, entry)
        await _configure(hass, form["flow_id"], {})
        await hass.async_block_till_done()

    assert CONF_API_KEY not in entry.data


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
    """Add a configured, loaded parent backend entry to hass."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={CONF_BACKEND_TYPE: backend_type}, **kwargs)
    entry.add_to_hass(hass)
    entry.mock_state(hass, config_entries.ConfigEntryState.LOADED)
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


async def test_subentry_aborts_when_parent_not_loaded(hass: HomeAssistant) -> None:
    """An agent can't be added under a parent entry that failed to load."""
    entry = _parent_entry(hass, BACKEND_CONVERSE)
    entry.mock_state(hass, config_entries.ConfigEntryState.SETUP_RETRY)
    registry = {BACKEND_CONVERSE: _sub_adapter(backend_type=BACKEND_CONVERSE)}

    with patch(_REGISTRY_PATH, registry):
        result = await _init_subentry(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"


async def test_reconfigure_drops_stale_llm_api_ids(hass: HomeAssistant) -> None:
    """A stored LLM API id that no longer exists is not offered back as a selected value."""
    entry = _parent_entry(
        hass,
        BACKEND_OPENAI_COMPAT,
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_CONVERSATION,
                "data": {CONF_LLM_HASS_API: [llm.LLM_API_ASSIST, "removed_mcp_server"]},
                "title": "Agent",
                "unique_id": None,
            }
        ],
    )
    registry = {BACKEND_OPENAI_COMPAT: _sub_adapter(backend_type=BACKEND_OPENAI_COMPAT, supports_ha_tools=True)}

    with patch(_REGISTRY_PATH, registry):
        form: dict[str, Any] = dict(await entry.start_subentry_reconfigure_flow(hass, next(iter(entry.subentries))))

    assert _suggested_values(form["data_schema"])[CONF_LLM_HASS_API] == [llm.LLM_API_ASSIST]


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


async def test_hanging_model_probe_falls_back_to_free_text(hass: HomeAssistant) -> None:
    """A backend that never answers the catalog probe can't freeze the form: it times out to text."""
    entry = _parent_entry(hass, BACKEND_OPENAI_COMPAT)

    class _HangingCatalog(_sub_adapter(backend_type=BACKEND_OPENAI_COMPAT)):  # type: ignore[misc]
        @classmethod
        async def async_list_models(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> list[str] | None:
            await asyncio.Event().wait()
            return None

    with (
        patch(_REGISTRY_PATH, {BACKEND_OPENAI_COMPAT: _HangingCatalog}),
        patch("custom_components.llm_middleman.config_flow.PROBE_TIMEOUT", 0.01),
    ):
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


# --- Dify agent options (LLMM-021) -------------------------------------------

_DIFY_FIELDS = {CONF_DIFY_INPUTS, CONF_DIFY_HA_CONTEXT, CONF_DIFY_USER_FROM_HA}
_FETCH_FORM_PATH = "custom_components.llm_middleman.config_flow.DifyAdapter.async_fetch_input_form"


def _dify_registry() -> dict[str, type[BackendAdapter]]:
    return {BACKEND_DIFY: _sub_adapter(backend_type=BACKEND_DIFY, supports_memory_scope=True)}


async def test_dify_agent_form_lists_declared_inputs(hass: HomeAssistant) -> None:
    """The Dify agent form adds the Dify options and names the app's declared inputs."""
    entry = _parent_entry(hass, BACKEND_DIFY)
    declared = {"city": {"variable": "city", "required": True}, "ha_language": {"variable": "ha_language"}}
    with patch(_REGISTRY_PATH, _dify_registry()), patch(_FETCH_FORM_PATH, AsyncMock(return_value=declared)):
        form = await _init_subentry(hass, entry)

    assert _schema_keys(form["data_schema"]) >= _DIFY_FIELDS
    assert form["description_placeholders"] == {"dify_app_inputs": "city (required), ha_language"}


async def test_dify_agent_form_when_parameters_unreachable(hass: HomeAssistant) -> None:
    entry = _parent_entry(hass, BACKEND_DIFY)
    with (
        patch(_REGISTRY_PATH, _dify_registry()),
        patch(_FETCH_FORM_PATH, AsyncMock(side_effect=BackendConnectionError("down"))),
    ):
        form = await _init_subentry(hass, entry)

    assert form["description_placeholders"]["dify_app_inputs"].startswith("unavailable")


async def test_dify_fields_absent_for_other_backends(hass: HomeAssistant) -> None:
    entry = _parent_entry(hass, BACKEND_CONVERSE)
    with patch(_REGISTRY_PATH, {BACKEND_CONVERSE: _sub_adapter(backend_type=BACKEND_CONVERSE)}):
        form = await _init_subentry(hass, entry)

    assert not _DIFY_FIELDS & _schema_keys(form["data_schema"])


async def test_dify_agent_options_saved(hass: HomeAssistant) -> None:
    entry = _parent_entry(hass, BACKEND_DIFY)
    options = {
        CONF_DIFY_INPUTS: {"city": "Berlin", "units": 2},
        CONF_DIFY_HA_CONTEXT: True,
        CONF_DIFY_USER_FROM_HA: True,
    }
    with patch(_REGISTRY_PATH, _dify_registry()), patch(_FETCH_FORM_PATH, AsyncMock(return_value={})):
        form = await _init_subentry(hass, entry)
        result = await _configure_subentry(hass, form["flow_id"], {CONF_NAME: "Jarvis", **options})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {key: result["data"][key] for key in options} == options


@pytest.mark.parametrize("bad_inputs", [["city", "Berlin"], {"city": {"nested": "x"}}, {"": "x"}])
async def test_dify_invalid_inputs_rejected(hass: HomeAssistant, bad_inputs: object) -> None:
    entry = _parent_entry(hass, BACKEND_DIFY)
    with patch(_REGISTRY_PATH, _dify_registry()), patch(_FETCH_FORM_PATH, AsyncMock(return_value={})):
        form = await _init_subentry(hass, entry)
        result = await _configure_subentry(hass, form["flow_id"], {CONF_NAME: "Jarvis", CONF_DIFY_INPUTS: bad_inputs})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_DIFY_INPUTS: "invalid_dify_inputs"}
    assert _suggested_values(result["data_schema"])[CONF_DIFY_INPUTS] == bad_inputs


# --- LangGraph-specific fields (LLMM-022) --------------------------------------------

_LANGGRAPH_AGENT_KEYS = {
    CONF_MULTITASK_STRATEGY,
    CONF_INPUT_MESSAGES_KEY,
    CONF_RESPONSE_NODE_FILTER,
    CONF_STATELESS_RUNS,
}


async def test_langgraph_connection_stores_auth_scheme(hass: HomeAssistant) -> None:
    registry = {BACKEND_LANGGRAPH: _fake_adapter()}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_backend_step(hass, BACKEND_LANGGRAPH)
        result = await _configure(
            hass,
            form["flow_id"],
            {
                CONF_BASE_URL: "http://lg.local:2024",
                CONF_API_KEY: "k",
                CONF_AUTH_SCHEME: "bearer",
                CONF_ASSISTANT_ID: "agent",
            },
        )

    assert result["data"][CONF_AUTH_SCHEME] == "bearer"


async def test_langgraph_auth_scheme_defaults_to_x_api_key(hass: HomeAssistant) -> None:
    registry = {BACKEND_LANGGRAPH: _fake_adapter()}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_backend_step(hass, BACKEND_LANGGRAPH)
        result = await _configure(hass, form["flow_id"], {CONF_BASE_URL: "http://lg.local:2024"})

    assert result["data"][CONF_AUTH_SCHEME] == "x_api_key"


async def test_langgraph_unknown_assistant_shows_field_error(hass: HomeAssistant) -> None:
    registry = {BACKEND_LANGGRAPH: _fake_adapter(AssistantNotFoundError("nope"))}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_backend_step(hass, BACKEND_LANGGRAPH)
        result = await _configure(
            hass, form["flow_id"], {CONF_BASE_URL: "http://lg.local:2024", CONF_ASSISTANT_ID: "x"}
        )

    assert result["errors"] == {CONF_ASSISTANT_ID: "assistant_not_found"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_langgraph_agent_options_are_offered(hass: HomeAssistant) -> None:
    entry = _parent_entry(hass, BACKEND_LANGGRAPH)
    registry = {BACKEND_LANGGRAPH: _sub_adapter(backend_type=BACKEND_LANGGRAPH, supports_memory_scope=True)}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_subentry(hass, entry)

    assert _schema_keys(form["data_schema"]) >= _LANGGRAPH_AGENT_KEYS
    assert _suggested_values(form["data_schema"])[CONF_MULTITASK_STRATEGY] == "rollback"


async def test_langgraph_agent_options_absent_for_other_backends(hass: HomeAssistant) -> None:
    entry = _parent_entry(hass, BACKEND_CONVERSE)
    registry = {BACKEND_CONVERSE: _sub_adapter(backend_type=BACKEND_CONVERSE, supports_memory_scope=True)}
    with patch(_REGISTRY_PATH, registry):
        form = await _init_subentry(hass, entry)

    assert not _LANGGRAPH_AGENT_KEYS & _schema_keys(form["data_schema"])


async def test_langgraph_agent_options_are_stored(hass: HomeAssistant) -> None:
    entry = _parent_entry(hass, BACKEND_LANGGRAPH)
    registry = {BACKEND_LANGGRAPH: _sub_adapter(backend_type=BACKEND_LANGGRAPH, supports_memory_scope=True)}
    submitted = {
        CONF_NAME: "Graph agent",
        CONF_MULTITASK_STRATEGY: "enqueue",
        CONF_INPUT_MESSAGES_KEY: "msgs",
        CONF_RESPONSE_NODE_FILTER: "respond",
        CONF_STATELESS_RUNS: True,
    }
    with patch(_REGISTRY_PATH, registry):
        form = await _init_subentry(hass, entry)
        result = await _configure_subentry(hass, form["flow_id"], submitted)

    assert {key: result["data"][key] for key in _LANGGRAPH_AGENT_KEYS} == {
        CONF_MULTITASK_STRATEGY: "enqueue",
        CONF_INPUT_MESSAGES_KEY: "msgs",
        CONF_RESPONSE_NODE_FILTER: "respond",
        CONF_STATELESS_RUNS: True,
    }


# --- preset-specific agent options read by the adapters ----------------------------

_PRESET_AGENT_KEYS = {
    BACKEND_OPENAI_COMPAT: {CONF_TEMPERATURE, CONF_TOP_P, CONF_MAX_TOKENS},
    BACKEND_OLLAMA: {CONF_NUM_CTX, CONF_KEEP_ALIVE, CONF_THINK},
    BACKEND_N8N: {CONF_INPUT_FIELD, CONF_OUTPUT_FIELD, CONF_SESSION_FIELD},
    BACKEND_CONVERSE: set[str](),
}
_ALL_PRESET_AGENT_KEYS: set[str] = set[str]().union(*_PRESET_AGENT_KEYS.values())


@pytest.mark.parametrize("backend_type", list(_PRESET_AGENT_KEYS))
async def test_preset_agent_options_offered_only_for_their_preset(hass: HomeAssistant, backend_type: str) -> None:
    entry = _parent_entry(hass, backend_type)
    with patch(_REGISTRY_PATH, {backend_type: _sub_adapter(backend_type=backend_type)}):
        form = await _init_subentry(hass, entry)

    assert _schema_keys(form["data_schema"]) & _ALL_PRESET_AGENT_KEYS == _PRESET_AGENT_KEYS[backend_type]


@pytest.mark.parametrize(
    ("backend_type", "submitted"),
    [
        (BACKEND_OPENAI_COMPAT, {CONF_TEMPERATURE: 0.7, CONF_TOP_P: 0.9, CONF_MAX_TOKENS: 256}),
        (BACKEND_OLLAMA, {CONF_NUM_CTX: 4096, CONF_KEEP_ALIVE: -1, CONF_THINK: True}),
        (BACKEND_N8N, {CONF_INPUT_FIELD: "q", CONF_OUTPUT_FIELD: "answer", CONF_SESSION_FIELD: "sid"}),
    ],
)
async def test_preset_agent_options_are_stored(
    hass: HomeAssistant, backend_type: str, submitted: dict[str, Any]
) -> None:
    entry = _parent_entry(hass, backend_type)
    with patch(_REGISTRY_PATH, {backend_type: _sub_adapter(backend_type=backend_type)}):
        form = await _init_subentry(hass, entry)
        result = await _configure_subentry(hass, form["flow_id"], {CONF_NAME: "Agent", **submitted})

    assert {key: result["data"][key] for key in submitted} == submitted


@pytest.mark.parametrize("backend_type", list(_PRESET_AGENT_KEYS))
async def test_preset_agent_options_unset_by_default(hass: HomeAssistant, backend_type: str) -> None:
    # Unset sampling/runtime options must stay absent so the backend's own default applies.
    entry = _parent_entry(hass, backend_type)
    with patch(_REGISTRY_PATH, {backend_type: _sub_adapter(backend_type=backend_type)}):
        form = await _init_subentry(hass, entry)
        result = await _configure_subentry(hass, form["flow_id"], {CONF_NAME: "Agent"})

    assert not {CONF_TEMPERATURE, CONF_TOP_P, CONF_MAX_TOKENS, CONF_NUM_CTX, CONF_KEEP_ALIVE, CONF_THINK} & set(
        result["data"]
    )


_STRINGS = json.loads((Path(__file__).parents[1] / "custom_components/llm_middleman/strings.json").read_text())


@pytest.mark.parametrize("backend_type", list(_PRESET_AGENT_KEYS))
async def test_every_agent_field_is_labelled_and_described(hass: HomeAssistant, backend_type: str) -> None:
    entry = _parent_entry(hass, backend_type)
    with patch(_REGISTRY_PATH, {backend_type: _sub_adapter(backend_type=backend_type)}):
        form = await _init_subentry(hass, entry)
    step = _STRINGS["config_subentries"]["conversation"]["step"]["set_options"]

    assert _schema_keys(form["data_schema"]) <= set(step["data"]) & set(step["data_description"])
