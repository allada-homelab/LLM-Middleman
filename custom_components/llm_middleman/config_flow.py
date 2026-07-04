"""Config flow for LLM Middleman.

Parent flow only (LLMM-006): step 1 picks a backend type from ``BACKEND_TO_CLS``;
step 2 is that backend's connection form, validated by the adapter's real-endpoint
probe (``async_validate_connection``). The per-agent conversation subentry flow is
LLMM-007. Two v0 defects are dropped here: URL-as-``unique_id`` (duplicate agents on
one backend are legitimate) and the base-URL reachability probe that validated
nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.helpers.selector import (
    SelectSelector,  # pyright: ignore[reportUnknownVariableType]
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,  # pyright: ignore[reportUnknownVariableType]
    TextSelectorConfig,
    TextSelectorType,
)

from .backends import BACKEND_TO_CLS
from .backends.base import BackendAuthError, BackendConnectionError
from .const import (
    BACKEND_CONVERSE,
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
    CONF_PASSWORD,
    CONF_TARGET_TYPE,
    CONF_TOKEN,
    CONF_USERNAME,
    CONF_WEBHOOK_URL,
    DOMAIN,
    N8N_AUTH_BASIC,
    N8N_AUTH_HEADER,
    N8N_AUTH_NONE,
    N8N_TARGET_CHAT_TRIGGER,
    N8N_TARGET_WEBHOOK,
)

# Fixed per-backend entry title. The human-facing agent name lives on the
# conversation subentry (LLMM-007), so the parent title is a stable backend label.
_BACKEND_TITLES = {
    BACKEND_OPENAI_COMPAT: "OpenAI-compatible",
    BACKEND_LANGGRAPH: "LangGraph",
    BACKEND_OLLAMA: "Ollama",
    BACKEND_CONVERSE: "Custom /v1/converse",
    BACKEND_N8N: "n8n",
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


class LLMMiddlemanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for LLM Middleman."""

    # VERSION 2 triggers async_migrate_entry (LLMM-013) for v0 (VERSION 1) entries.
    VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow. The backend type is set in step 1 (or reconfigure)."""
        # Empty until a backend is picked; every connection step runs only after it is set.
        self._backend_type: str = ""

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

    async def _async_route_to_connection_step(self) -> ConfigFlowResult:
        """Dispatch to the connection step for the selected backend type."""
        steps: dict[str, Callable[[], Awaitable[ConfigFlowResult]]] = {
            BACKEND_OPENAI_COMPAT: self.async_step_openai_compat,
            BACKEND_LANGGRAPH: self.async_step_langgraph,
            BACKEND_OLLAMA: self.async_step_ollama,
            BACKEND_CONVERSE: self.async_step_converse,
            BACKEND_N8N: self.async_step_n8n,
        }
        return await steps[self._backend_type]()

    async def async_step_openai_compat(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """OpenAI-compatible connection step."""
        return await self._async_connection_step(_openai_compat_schema(), url_key=CONF_BASE_URL, user_input=user_input)

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

    async def _async_connection_step(
        self,
        schema: vol.Schema,
        *,
        url_key: str | None,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Shared per-backend connection handler: normalize, probe, create/update.

        ``url_key`` names the base-URL field to strip a trailing slash from (a slash
        double-slashes ``/v1//models`` → 404); ``None`` leaves an opaque URL (n8n
        webhook) untouched. Validation calls the adapter's real-endpoint probe and
        maps its typed errors; ``BackendAuthError`` subclasses ``BackendConnectionError``
        so it is caught first.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            data = dict(user_input)
            if url_key is not None:
                data[url_key] = str(data[url_key]).rstrip("/")

            adapter_cls = BACKEND_TO_CLS[self._backend_type]
            try:
                await adapter_cls.async_validate_connection(self.hass, data)
            except BackendAuthError:
                errors["base"] = "invalid_auth"
            except BackendConnectionError:
                errors["base"] = "cannot_connect"
            else:
                entry_data = {CONF_BACKEND_TYPE: self._backend_type, **data}
                if self.source == SOURCE_RECONFIGURE:
                    return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data=entry_data)
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
