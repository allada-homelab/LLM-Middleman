"""LLM Middleman integration for Home Assistant.

A backend-agnostic, text-only passthrough conversation agent: it forwards each
Assist turn to an external LLM backend (selected via a preset) and streams the reply
back into the pipeline. All intelligence (tools, memory, providers) lives in the
external service; this integration owns only the HA plumbing.

Setup builds the backend adapter once — via the ``BACKEND_TO_CLS`` factory keyed on
the parent entry's ``CONF_BACKEND_TYPE`` — and stores the shared instance in
``entry.runtime_data``. The conversation platform drives that one adapter per turn.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .backends import BackendAdapter, get_backend_cls
from .const import CONF_BACKEND_TYPE

PLATFORMS = (Platform.CONVERSATION,)

type LLMMiddlemanConfigEntry = ConfigEntry[BackendAdapter]


async def async_setup_entry(hass: HomeAssistant, entry: LLMMiddlemanConfigEntry) -> bool:
    """Build the backend adapter and forward the conversation platform."""
    adapter_cls = get_backend_cls(entry.data[CONF_BACKEND_TYPE])
    # entry_id rides along so adapters needing a per-entry discriminator (the
    # langgraph thread-map Store) never fall back to a shareable base_url slug.
    connection_data = {**entry.data, "entry_id": entry.entry_id}
    entry.runtime_data = adapter_cls(hass, async_create_clientsession(hass), connection_data)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LLMMiddlemanConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
