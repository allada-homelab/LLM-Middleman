"""Config flow for LLM Middleman."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import CONF_NAME, CONF_SYSTEM_PROMPT, CONF_TOKEN, CONF_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="LLM Middleman"): TextSelector(),
        vol.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
        vol.Optional(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Optional(CONF_SYSTEM_PROMPT): TemplateSelector(),
    }
)


async def _async_check_reachable(hass: Any, url: str) -> bool:
    """Cheap reachability probe. Only connection-level failures count as unreachable.

    A non-2xx HTTP status is fine — the base URL may legitimately 404/405 on GET
    while ``/v1/converse`` works, so we only fail on an actual transport error.
    """
    session = async_get_clientsession(hass)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)):
            return True
    except (aiohttp.ClientConnectionError, TimeoutError):
        return False
    except aiohttp.ClientError:
        # An HTTP-level error means the host answered — treat as reachable.
        return True


class LLMMiddlemanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for LLM Middleman."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()

            if await _async_check_reachable(self.hass, url):
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_DATA_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle reconfiguration of an existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            if await _async_check_reachable(self.hass, url):
                return self.async_update_reload_and_abort(entry, title=user_input[CONF_NAME], data=user_input)
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_DATA_SCHEMA, user_input or entry.data),
            errors=errors,
        )
