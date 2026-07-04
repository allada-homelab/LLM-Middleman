"""LLM Middleman integration for Home Assistant.

A thin, text-only passthrough conversation agent: it forwards each Assist turn to
an external LLM agent over the ``/v1/converse`` SSE contract and streams the reply
back into the pipeline. All intelligence (tools, memory, providers) lives in the
external service; this integration owns only the HA plumbing.
"""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

PLATFORMS = (Platform.CONVERSATION,)

type LLMMiddlemanConfigEntry = ConfigEntry[aiohttp.ClientSession]


async def async_setup_entry(hass: HomeAssistant, entry: LLMMiddlemanConfigEntry) -> bool:
    """Set up LLM Middleman from a config entry."""
    entry.runtime_data = async_create_clientsession(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LLMMiddlemanConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
