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

from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_PROMPT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .backends import BackendAdapter, get_backend_cls
from .const import (
    BACKEND_CONVERSE,
    CONF_BACKEND_TYPE,
    CONF_BASE_URL,
    CONF_NAME,
    CONF_SYSTEM_PROMPT,
    CONF_TIMEOUT,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    SUBENTRY_TYPE_CONVERSATION,
)

PLATFORMS = (Platform.CONVERSATION,)

# Default agent name when a v0 entry carries no ``name`` (v0's title fallback).
_DEFAULT_AGENT_NAME = "LLM Middleman"

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


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a v0 (VERSION 1) entry into the v1 parent + conversation subentry model.

    v0 was a single flat entry ``{url, token, system_prompt, name}`` with one
    conversation entity keyed on ``entry.entry_id``. v1 keeps the v0 ``/v1/converse``
    backend as the ``converse`` preset, so the flat data becomes a converse parent plus
    one ``conversation`` subentry, and the existing entity is re-pointed onto the
    subentry so its ``entity_id`` (and the automations/exposure that reference it)
    survives the upgrade. Mirrors core ``openai_conversation``'s 2025.7 migration.

    A ``version > 2`` entry (a downgrade) is refused rather than corrupted.
    """
    if entry.version > 2:
        return False
    if entry.version == 1:
        old = entry.data
        # Name lives on the subentry as its title (the canonical location the entity's
        # device name reads), exactly as the add-agent subentry flow stores it.
        name = old.get(CONF_NAME) or _DEFAULT_AGENT_NAME
        subentry = ConfigSubentry(
            data=MappingProxyType(
                {
                    CONF_PROMPT: old.get(CONF_SYSTEM_PROMPT),
                    CONF_TIMEOUT: DEFAULT_TIMEOUT,
                }
            ),
            subentry_type=SUBENTRY_TYPE_CONVERSATION,
            title=name,
            unique_id=None,
        )
        subentry_id = subentry.subentry_id
        hass.config_entries.async_add_subentry(entry, subentry)

        # Entity-id continuity: re-key the v0 entity from entry_id → subentry_id and
        # attach it to the new subentry; changing unique_id in place keeps entity_id.
        ent_reg = er.async_get(hass)
        if entity_id := ent_reg.async_get_entity_id("conversation", DOMAIN, entry.entry_id):
            ent_reg.async_update_entity(
                entity_id,
                config_subentry_id=subentry_id,
                new_unique_id=subentry_id,
            )

        # Move the v0 device (keyed on entry_id) onto the subentry so the entity's
        # device link (keyed on subentry_id in v1) is preserved.
        dev_reg = dr.async_get(hass)
        if device := dev_reg.async_get_device(identifiers={(DOMAIN, entry.entry_id)}):
            dev_reg.async_update_device(
                device.id,
                new_identifiers={(DOMAIN, subentry_id)},
                add_config_subentry_id=subentry_id,
                add_config_entry_id=entry.entry_id,
            )
            dev_reg.async_update_device(
                device.id,
                remove_config_entry_id=entry.entry_id,
                remove_config_subentry_id=None,
            )

        # Replace the flat data with converse connection data and bump the version.
        new_data: dict[str, Any] = {
            CONF_BACKEND_TYPE: BACKEND_CONVERSE,
            CONF_BASE_URL: old[CONF_URL],
            CONF_TOKEN: old.get(CONF_TOKEN),
        }
        hass.config_entries.async_update_entry(entry, data=new_data, version=2)
    return True
