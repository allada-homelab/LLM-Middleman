"""Config flow for LLM Middleman.

Parent flow only (LLMM-006): step 1 picks a backend type from ``BACKEND_TO_CLS``;
step 2 is that backend's connection form, validated by the adapter's real-endpoint
probe (``async_validate_connection``). The per-agent conversation subentry flow is
LLMM-007. Two v0 defects are dropped here: URL-as-``unique_id`` (duplicate agents on
one backend are legitimate) and the base-URL reachability probe that validated
nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_LLM_HASS_API, CONF_NAME, CONF_PROMPT
from homeassistant.core import callback
from homeassistant.helpers import llm
from homeassistant.helpers.selector import (
    NumberSelector,  # pyright: ignore[reportUnknownVariableType]
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,  # pyright: ignore[reportUnknownVariableType]
    SelectSelectorConfig,
    SelectSelectorMode,
    TemplateSelector,  # pyright: ignore[reportUnknownVariableType]
    TextSelector,  # pyright: ignore[reportUnknownVariableType]
    TextSelectorConfig,
    TextSelectorType,
)

from .backends import BACKEND_TO_CLS
from .backends.base import BackendAdapter, BackendAuthError, BackendConnectionError
from .const import (
    BACKEND_CONVERSE,
    BACKEND_DIFY,
    BACKEND_LANGGRAPH,
    BACKEND_N8N,
    BACKEND_OLLAMA,
    BACKEND_OPENAI_COMPAT,
    CONF_API_KEY,
    CONF_ASSISTANT_ID,
    CONF_AUTH_TYPE,
    CONF_BACKEND_TYPE,
    CONF_BASE_URL,
    CONF_HEADER_NAME,
    CONF_HEADER_VALUE,
    CONF_MAX_HISTORY,
    CONF_MEMORY_SCOPE,
    CONF_MODEL,
    CONF_PASSWORD,
    CONF_TARGET_TYPE,
    CONF_TIMEOUT,
    CONF_TOKEN,
    CONF_USERNAME,
    CONF_WEBHOOK_URL,
    DEFAULT_MAX_HISTORY,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MEMORY_SCOPE_AGENT,
    MEMORY_SCOPE_CONVERSATION,
    MEMORY_SCOPE_DEVICE,
    N8N_AUTH_BASIC,
    N8N_AUTH_HEADER,
    N8N_AUTH_NONE,
    N8N_TARGET_CHAT_TRIGGER,
    N8N_TARGET_WEBHOOK,
    PROBE_TIMEOUT,
    SUBENTRY_TYPE_CONVERSATION,
)

if TYPE_CHECKING:
    # HA >= 2026.9 aliases `voluptuous` to probatio at runtime and types flow schemas as
    # probatio's; type-check against that. Older HA (no probatio) gets real voluptuous.
    import probatio as vol
else:
    import voluptuous as vol

_LOGGER = logging.getLogger(__name__)

# Default display name for a newly added conversation agent.
_DEFAULT_AGENT_NAME = "LLM Middleman"

# Fixed per-backend entry title. The human-facing agent name lives on the
# conversation subentry (LLMM-007), so the parent title is a stable backend label.
_BACKEND_TITLES = {
    BACKEND_OPENAI_COMPAT: "OpenAI-compatible",
    BACKEND_LANGGRAPH: "LangGraph",
    BACKEND_OLLAMA: "Ollama",
    BACKEND_CONVERSE: "Custom /v1/converse",
    BACKEND_N8N: "n8n",
    BACKEND_DIFY: "Dify",
}


def _url_field() -> Any:
    # HA's selector classes are only partially typed (Selector[Unknown]); these
    # builders erase to Any so the per-backend schemas below stay readable.
    return TextSelector(TextSelectorConfig(type=TextSelectorType.URL))  # pyright: ignore[reportUnknownVariableType]


def _password_field() -> Any:
    return TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))  # pyright: ignore[reportUnknownVariableType]


def _openai_compat_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_BASE_URL): _url_field(),
            vol.Optional(CONF_API_KEY): _password_field(),
        }
    )


def _langgraph_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_BASE_URL): _url_field(),
            vol.Optional(CONF_API_KEY): _password_field(),
            vol.Required(CONF_ASSISTANT_ID, default="agent"): TextSelector(),
        }
    )


def _ollama_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_BASE_URL): _url_field(),
            vol.Optional(CONF_API_KEY): _password_field(),
        }
    )


def _converse_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_BASE_URL): _url_field(),
            vol.Optional(CONF_TOKEN): _password_field(),
        }
    )


def _dify_schema() -> vol.Schema:
    return vol.Schema(
        {
            # Unlike the other presets, Dify's app key is mandatory, not optional.
            vol.Required(CONF_BASE_URL): _url_field(),
            vol.Required(CONF_API_KEY): _password_field(),
        }
    )


def _n8n_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_WEBHOOK_URL): _url_field(),
            vol.Required(CONF_TARGET_TYPE, default=N8N_TARGET_CHAT_TRIGGER): SelectSelector(
                SelectSelectorConfig(
                    options=[N8N_TARGET_CHAT_TRIGGER, N8N_TARGET_WEBHOOK],
                    translation_key="target_type",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(CONF_AUTH_TYPE, default=N8N_AUTH_NONE): SelectSelector(
                SelectSelectorConfig(
                    options=[N8N_AUTH_NONE, N8N_AUTH_BASIC, N8N_AUTH_HEADER],
                    translation_key="auth_type",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(CONF_USERNAME): TextSelector(),
            vol.Optional(CONF_PASSWORD): _password_field(),
            vol.Optional(CONF_HEADER_NAME): TextSelector(),
            vol.Optional(CONF_HEADER_VALUE): _password_field(),
        }
    )


def _reauth_schema(data: Mapping[str, Any]) -> vol.Schema | None:
    """Credential-only schema for an entry's backend; ``None`` when it has no credential.

    Mirrors each connection schema's secret field(s); n8n's depend on its auth type, and
    an unauthenticated n8n webhook has nothing to re-enter.
    """
    backend_type = data[CONF_BACKEND_TYPE]
    if backend_type == BACKEND_N8N:
        auth_type = data.get(CONF_AUTH_TYPE, N8N_AUTH_NONE)
        if auth_type == N8N_AUTH_BASIC:
            return vol.Schema(
                {vol.Required(CONF_USERNAME): TextSelector(), vol.Required(CONF_PASSWORD): _password_field()}
            )
        if auth_type == N8N_AUTH_HEADER:
            return vol.Schema(
                {vol.Required(CONF_HEADER_NAME): TextSelector(), vol.Required(CONF_HEADER_VALUE): _password_field()}
            )
        return None
    if backend_type == BACKEND_CONVERSE:
        return vol.Schema({vol.Optional(CONF_TOKEN): _password_field()})
    if backend_type == BACKEND_DIFY:
        return vol.Schema({vol.Required(CONF_API_KEY): _password_field()})
    return vol.Schema({vol.Optional(CONF_API_KEY): _password_field()})


class LLMMiddlemanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for LLM Middleman."""

    # VERSION 2 triggers async_migrate_entry (LLMM-013) for v0 (VERSION 1) entries.
    VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow. The backend type is set in step 1 (or reconfigure)."""
        # Empty until a backend is picked; every connection step runs only after it is set.
        self._backend_type: str = ""

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        """Expose the conversation-agent subentry type for every backend (LLMM-007)."""
        return {SUBENTRY_TYPE_CONVERSATION: ConversationSubentryFlowHandler}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: choose the backend type, then route to its connection step."""
        if user_input is not None:
            self._backend_type = user_input[CONF_BACKEND_TYPE]
            return await self._async_route_to_connection_step()

        schema = vol.Schema(
            {
                vol.Required(CONF_BACKEND_TYPE): SelectSelector(
                    SelectSelectorConfig(
                        options=list(BACKEND_TO_CLS),
                        translation_key="backend_type",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Reconfigure an existing entry: backend type is fixed, re-run its step."""
        self._backend_type = self._get_reconfigure_entry().data[CONF_BACKEND_TYPE]
        return await self._async_route_to_connection_step()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauth after the backend rejected the entry's credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Re-ask only the credential field(s), validate them, then update the entry."""
        entry = self._get_reauth_entry()
        schema = _reauth_schema(entry.data)
        if schema is None:
            return self.async_abort(reason="reauth_not_supported")
        errors: dict[str, str] = {}

        if user_input is not None:
            fields = {str(marker.schema) for marker in schema.schema}
            # Drop every credential field first so a blank (omitted) optional key is cleared.
            data = {key: value for key, value in entry.data.items() if key not in fields} | user_input
            backend_type = data[CONF_BACKEND_TYPE]
            try:
                await BACKEND_TO_CLS[backend_type].async_validate_connection(self.hass, data)
            except BackendAuthError:
                errors["base"] = "invalid_auth"
            except BackendConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating the %s reauth", backend_type)
                errors["base"] = "unknown"
            else:
                if entry.update_listeners:
                    # Loaded entry: its update listener reloads; a second reload is deprecated.
                    return self.async_update_and_abort(entry, data=data)
                # Setup failed (ConfigEntryAuthFailed) before the listener was added: reload here.
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(schema, user_input) if user_input else schema,
            errors=errors,
        )

    async def _async_route_to_connection_step(self) -> ConfigFlowResult:
        """Dispatch to the connection step for the selected backend type."""
        steps: dict[str, Callable[[], Awaitable[ConfigFlowResult]]] = {
            BACKEND_OPENAI_COMPAT: self.async_step_openai_compat,
            BACKEND_LANGGRAPH: self.async_step_langgraph,
            BACKEND_OLLAMA: self.async_step_ollama,
            BACKEND_CONVERSE: self.async_step_converse,
            BACKEND_N8N: self.async_step_n8n,
            BACKEND_DIFY: self.async_step_dify,
        }
        return await steps[self._backend_type]()

    async def async_step_openai_compat(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """OpenAI-compatible connection step.

        The adapter hardcodes the ``/v1`` prefix, so it wants the server *root*. Users
        routinely paste the conventional OpenAI-style ``…/v1`` base URL (that is OpenAI's
        own base URL); left as-is it double-paths to ``/v1/v1/models`` and 404s. Strip a
        trailing ``/v1`` so both the root and the ``/v1`` form resolve.
        """
        return await self._async_connection_step(
            _openai_compat_schema(), url_key=CONF_BASE_URL, user_input=user_input, strip_v1_suffix=True
        )

    async def async_step_langgraph(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """LangGraph connection step."""
        return await self._async_connection_step(_langgraph_schema(), url_key=CONF_BASE_URL, user_input=user_input)

    async def async_step_ollama(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ollama connection step."""
        return await self._async_connection_step(_ollama_schema(), url_key=CONF_BASE_URL, user_input=user_input)

    async def async_step_converse(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Custom /v1/converse connection step."""
        return await self._async_connection_step(_converse_schema(), url_key=CONF_BASE_URL, user_input=user_input)

    async def async_step_n8n(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """n8n connection step. The webhook URL is opaque and stored verbatim."""
        return await self._async_connection_step(_n8n_schema(), url_key=None, user_input=user_input)

    async def async_step_dify(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Dify connection step.

        The API root includes ``/v1`` (Dify Cloud is ``https://api.dify.ai/v1``); accept
        both the root and the ``/v1`` form by appending ``/v1`` when absent, so the stored
        base URL is always the ``/v1`` root the adapter posts against.
        """
        return await self._async_connection_step(
            _dify_schema(), url_key=CONF_BASE_URL, user_input=user_input, append_v1_suffix=True
        )

    async def _async_connection_step(
        self,
        schema: vol.Schema,
        *,
        url_key: str | None,
        user_input: dict[str, Any] | None,
        strip_v1_suffix: bool = False,
        append_v1_suffix: bool = False,
    ) -> ConfigFlowResult:
        """Shared per-backend connection handler: normalize, probe, create/update.

        ``url_key`` names the base-URL field to strip a trailing slash from (a slash
        double-slashes ``/v1//models`` → 404); ``None`` leaves an opaque URL (n8n
        webhook) untouched. ``strip_v1_suffix`` additionally drops one trailing ``/v1``
        for backends that hardcode the ``/v1`` prefix (openai_compat), so the stored
        base URL is always the server root; ``append_v1_suffix`` is the inverse — it
        adds ``/v1`` when absent, so the stored base URL is always the ``/v1`` root
        (dify, whose API convention includes ``/v1``). Validation calls the adapter's
        real-endpoint probe and maps its typed errors; ``BackendAuthError`` subclasses
        ``BackendConnectionError`` so it is caught first.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            data = dict(user_input)
            if url_key is not None:
                normalized = str(data[url_key]).rstrip("/")
                if strip_v1_suffix:
                    normalized = normalized.removesuffix("/v1")
                if append_v1_suffix and not normalized.endswith("/v1"):
                    normalized += "/v1"
                data[url_key] = normalized

            adapter_cls = BACKEND_TO_CLS[self._backend_type]
            try:
                await adapter_cls.async_validate_connection(self.hass, data)
            except BackendAuthError:
                errors["base"] = "invalid_auth"
            except BackendConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating the %s connection", self._backend_type)
                errors["base"] = "unknown"
            else:
                entry_data = {CONF_BACKEND_TYPE: self._backend_type, **data}
                if self.source != SOURCE_RECONFIGURE:
                    # Only an identical connection is a duplicate: one URL can back distinct
                    # connections (Dify Cloud apps differ only by key; LangGraph by assistant).
                    self._async_abort_entries_match(entry_data)
                if self.source == SOURCE_RECONFIGURE:
                    # The entry's update listener reloads it; a second reload here is deprecated.
                    return self.async_update_and_abort(self._get_reconfigure_entry(), data=entry_data)
                return self.async_create_entry(title=_BACKEND_TITLES[self._backend_type], data=entry_data)

        return self.async_show_form(
            step_id=self._backend_type,
            data_schema=self._prefilled(schema, user_input),
            errors=errors,
        )

    def _prefilled(self, schema: vol.Schema, user_input: Mapping[str, Any] | None) -> vol.Schema:
        """Prefill the schema: echo re-submitted input, else an entry on reconfigure."""
        if user_input is not None:
            return self.add_suggested_values_to_schema(schema, user_input)
        if self.source == SOURCE_RECONFIGURE:
            return self.add_suggested_values_to_schema(schema, self._get_reconfigure_entry().data)
        return schema


class ConversationSubentryFlowHandler(ConfigSubentryFlow):
    """Per-agent conversation subentry flow (LLMM-007).

    One typed ``conversation`` subentry per agent under a parent backend entry, so a
    single backend/credential can back several agents with different prompts/models.
    Mirrors the core ollama template: ``async_step_user`` (new agent) and
    ``async_step_reconfigure`` (edit) both alias one shared ``set_options`` step; the
    subentry ``data`` produced here is exactly what the entity (LLMM-005) reads.
    """

    @property
    def _is_new(self) -> bool:
        """True for the add-agent flow, False when reconfiguring an existing one."""
        return self.source == SOURCE_USER

    async def async_step_set_options(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Show/handle the common agent options form (create vs reconfigure)."""
        entry = self._get_entry()
        # Core openai/ollama pattern: agents are only managed under a loaded connection.
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        adapter_cls = BACKEND_TO_CLS[entry.data[CONF_BACKEND_TYPE]]

        if user_input is not None:
            if self._is_new:
                # The name is the subentry title, not stored option data.
                return self.async_create_entry(title=user_input.pop(CONF_NAME), data=user_input)
            return self.async_update_and_abort(entry, self._get_reconfigure_subentry(), data=user_input)

        options = {} if self._is_new else dict(self._get_reconfigure_subentry().data)
        return self.async_show_form(
            step_id="set_options",
            data_schema=await self._build_schema(adapter_cls, entry, options),
        )

    async_step_user = async_step_set_options
    async_step_reconfigure = async_step_set_options

    async def _build_schema(
        self,
        adapter_cls: type[BackendAdapter],
        entry: ConfigEntry,
        options: Mapping[str, Any],
    ) -> vol.Schema:
        """Build the common per-agent schema; ``options`` prefills each field.

        Every field is a per-agent setting. Capability ClassVars on the adapter gate
        the model dropdown (catalog backends only), ``CONF_MEMORY_SCOPE`` (stateful
        backends only), and ``CONF_LLM_HASS_API`` (tool-capable backends only).
        """
        schema: dict[Any, Any] = {}

        # The agent's display name — new agents only; reconfigure keeps the title.
        if self._is_new:
            schema[vol.Required(CONF_NAME, default=_DEFAULT_AGENT_NAME)] = TextSelector()

        # Per-agent system prompt (a Jinja template; converse/n8n also forward it,
        # langgraph prepends it under MessagesState).
        schema[
            vol.Optional(
                CONF_PROMPT,
                description={"suggested_value": options.get(CONF_PROMPT, llm.DEFAULT_INSTRUCTIONS_PROMPT)},
            )
        ] = TemplateSelector()

        # Model dropdown only for backends with a catalog (openai/ollama); free-text on
        # probe failure; absent entirely when the backend has no catalog.
        model_selector = await self._model_selector(adapter_cls, entry)
        if model_selector is not None:
            schema[vol.Optional(CONF_MODEL, description={"suggested_value": options.get(CONF_MODEL)})] = model_selector

        # Stateless-replay trim depth; harmless for full-replay backends.
        schema[
            vol.Optional(
                CONF_MAX_HISTORY,
                description={"suggested_value": options.get(CONF_MAX_HISTORY, DEFAULT_MAX_HISTORY)},
            )
        ] = NumberSelector(NumberSelectorConfig(min=0, step=1, mode=NumberSelectorMode.BOX))

        # Per-turn total deadline (seconds); voice UX degrades past it.
        schema[
            vol.Optional(
                CONF_TIMEOUT,
                description={"suggested_value": options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)},
            )
        ] = NumberSelector(NumberSelectorConfig(min=10, max=300, step=1, mode=NumberSelectorMode.BOX))

        # Session-key scope — only stateful backends can honor it; stateless backends
        # can't resurrect history HA discarded, so the option is hidden for them.
        if adapter_cls.supports_memory_scope:
            schema[
                vol.Optional(
                    CONF_MEMORY_SCOPE,
                    description={"suggested_value": options.get(CONF_MEMORY_SCOPE, MEMORY_SCOPE_CONVERSATION)},
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[MEMORY_SCOPE_CONVERSATION, MEMORY_SCOPE_DEVICE, MEMORY_SCOPE_AGENT],
                    translation_key="memory_scope",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        # HA LLM API(s) whose tools the backend may call — only for tool-capable
        # backends. Multi-select storing list[str]; async_provide_llm_data accepts a
        # list and pulls in the tools of every named API (device control + any HA
        # MCP-client entry). Unset/empty means "no tools" — no default is stored.
        if adapter_cls.supports_ha_tools:
            apis = llm.async_get_apis(self.hass)
            # Drop stored ids of APIs that no longer exist (e.g. a removed MCP server) so
            # the form doesn't preselect an option it can't render (core openai pattern).
            valid_ids = {api.id for api in apis}
            suggested_apis = options.get(CONF_LLM_HASS_API)
            if suggested_apis:
                suggested_apis = [api_id for api_id in suggested_apis if api_id in valid_ids]
            schema[
                vol.Optional(
                    CONF_LLM_HASS_API,
                    description={"suggested_value": suggested_apis},
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=api.id, label=api.name) for api in apis],
                    multiple=True,
                )
            )

        return vol.Schema(schema)

    async def _model_selector(self, adapter_cls: type[BackendAdapter], entry: ConfigEntry) -> Any:
        """Return the model field selector, or ``None`` for catalog-less backends.

        Probes the backend's model catalog. Catalog present → a dropdown with a
        free-text fallback (``custom_value``). Catalog-less backend → ``None`` (no
        field). Probe failure → a plain text field, so the form still renders.
        """
        try:
            # Bounded well under the probe's own 60 s so a dead backend can't freeze the form.
            async with asyncio.timeout(PROBE_TIMEOUT):
                models = await adapter_cls.async_list_models(self.hass, entry.data)
        except Exception:  # any probe failure (timeout included) must still render the form (free-text)
            _LOGGER.debug("Model probe failed for %s; falling back to free text", adapter_cls.backend_type)
            return TextSelector()  # pyright: ignore[reportUnknownVariableType]
        if models is None:
            return None
        return SelectSelector(  # pyright: ignore[reportUnknownVariableType]
            SelectSelectorConfig(options=models, custom_value=True, mode=SelectSelectorMode.DROPDOWN)
        )
