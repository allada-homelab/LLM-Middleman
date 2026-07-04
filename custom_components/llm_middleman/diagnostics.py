"""Diagnostics support for LLM Middleman.

HA auto-discovers ``async_get_config_entry_diagnostics`` by module presence — no
platform registration in ``__init__.py`` is needed. The dump mirrors core's
``anthropic``/``openai_conversation`` diagnostics: parent metadata, redacted parent
``data``/``options``, each conversation subentry's redacted ``data``, and the entry's
entities. Every credential, URL, and prompt is redacted (see ``TO_REDACT``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# async_redact_data is an untyped overload upstream (Mapping[Unknown, Unknown]); its
# results flow into this module's dict[str, Any] return, so no type safety is lost.
from homeassistant.components.diagnostics import async_redact_data  # pyright: ignore[reportUnknownVariableType]
from homeassistant.const import CONF_API_KEY, CONF_PROMPT, CONF_URL
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BACKEND_TYPE,
    CONF_BASE_URL,
    CONF_HEADER_VALUE,
    CONF_PASSWORD,
    CONF_SYSTEM_PROMPT,
    CONF_TOKEN,
    CONF_USERNAME,
    CONF_WEBHOOK_URL,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import LLMMiddlemanConfigEntry

# Every secret/PII/topology field across all five backend presets' parent and
# subentry schemas. Over-redaction is safe; a leaked token/URL/prompt is not.
TO_REDACT = {
    # Auth credentials (openai_compat/langgraph/ollama key, converse bearer, n8n
    # basic-auth user+password, n8n custom-header value).
    CONF_API_KEY,
    CONF_TOKEN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_HEADER_VALUE,
    # URLs — base URLs leak LAN topology; a webhook URL is effectively a secret.
    CONF_URL,
    CONF_BASE_URL,
    CONF_WEBHOOK_URL,
    # Prompts may carry personal context. Both keys are live: config-flow/entity
    # read ``CONF_PROMPT`` ("prompt"); langgraph/n8n adapters read
    # ``CONF_SYSTEM_PROMPT`` ("system_prompt").
    CONF_PROMPT,
    CONF_SYSTEM_PROMPT,
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: LLMMiddlemanConfigEntry) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""

    return {
        "title": entry.title,
        "entry_id": entry.entry_id,
        "entry_version": f"{entry.version}.{entry.minor_version}",
        "state": entry.state.value,
        # Backend type is useful and never sensitive (also present in redacted data).
        "backend_type": entry.data.get(CONF_BACKEND_TYPE),
        "data": async_redact_data(entry.data, TO_REDACT),
        "options": async_redact_data(entry.options, TO_REDACT),
        "subentries": {
            subentry.subentry_id: {
                "title": subentry.title,
                "subentry_type": subentry.subentry_type,
                "data": async_redact_data(subentry.data, TO_REDACT),
            }
            for subentry in entry.subentries.values()
        },
        "entities": {
            entity_entry.entity_id: entity_entry.extended_dict
            for entity_entry in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        },
    }
