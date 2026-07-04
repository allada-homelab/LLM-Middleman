"""Tests for the redacted config-entry diagnostics dump (LLMM-016).

The per-preset matrix is the guard the ticket asks for: for every backend preset a
config entry is built with known dummy secrets, and the dump must show ``**REDACTED**``
in place of every credential/URL/prompt and never leak a raw value. One test also
exercises the real HTTP download path to prove HA auto-discovers ``diagnostics.py``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_API_KEY, CONF_PROMPT, CONF_URL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,  # pyright: ignore[reportUnknownVariableType]
)

# ClientSessionGenerator is a partially-unknown TypeAlias upstream (TestClient[Unknown]).
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,  # pyright: ignore[reportUnknownVariableType]
)

from custom_components.llm_middleman.const import (
    BACKEND_CONVERSE,
    BACKEND_LANGGRAPH,
    BACKEND_N8N,
    BACKEND_OLLAMA,
    BACKEND_OPENAI_COMPAT,
    CONF_ASSISTANT_ID,
    CONF_AUTH_TYPE,
    CONF_BACKEND_TYPE,
    CONF_BASE_URL,
    CONF_HEADER_NAME,
    CONF_HEADER_VALUE,
    CONF_MODEL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_SYSTEM_PROMPT,
    CONF_TARGET_TYPE,
    CONF_TOKEN,
    CONF_USERNAME,
    CONF_WEBHOOK_URL,
    DOMAIN,
    N8N_AUTH_BASIC,
    N8N_TARGET_WEBHOOK,
)
from custom_components.llm_middleman.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import build_config_entry
from .test_conversation import FakeAdapter

REDACTED = "**REDACTED**"

# Dummy secret literals — none of these may appear anywhere in the JSON dump.
_SECRETS = {
    "api_key": "SECRET_API_KEY",
    "token": "SECRET_TOKEN",
    "password": "SECRET_PASSWORD",
    "header_value": "SECRET_HEADER_VALUE",
    "username": "secret-user",
    "base_url": "http://backend.local:8000",
    "webhook_url": "https://n8n.local/webhook/abc/chat",
    "url": "http://legacy.local:9000",
    "prompt": "PROMPT-my-name-is-Ada",
    "system_prompt": "SYSPROMPT-personal-context",
}

# Non-sensitive values that MUST survive unredacted.
_NON_SECRET_MODEL = "gpt-4o-mini"
_NON_SECRET_ASSISTANT = "my-graph"
_NON_SECRET_HEADER_NAME = "X-Api-Key"

# Per-preset parent connection data, keyed on backend_type. Each carries exactly the
# secret/URL fields that preset's config flow stores (plus the legacy CONF_URL on
# converse to prove the v0 key is still redacted).
_PARENT_DATA: dict[str, dict[str, object]] = {
    BACKEND_OPENAI_COMPAT: {
        CONF_BASE_URL: _SECRETS["base_url"],
        CONF_API_KEY: _SECRETS["api_key"],
    },
    BACKEND_LANGGRAPH: {
        CONF_BASE_URL: _SECRETS["base_url"],
        CONF_API_KEY: _SECRETS["api_key"],
        CONF_ASSISTANT_ID: _NON_SECRET_ASSISTANT,
    },
    BACKEND_OLLAMA: {
        CONF_BASE_URL: _SECRETS["base_url"],
        CONF_API_KEY: _SECRETS["api_key"],
    },
    BACKEND_CONVERSE: {
        CONF_BASE_URL: _SECRETS["base_url"],
        CONF_TOKEN: _SECRETS["token"],
        CONF_URL: _SECRETS["url"],
    },
    BACKEND_N8N: {
        CONF_WEBHOOK_URL: _SECRETS["webhook_url"],
        CONF_TARGET_TYPE: N8N_TARGET_WEBHOOK,
        CONF_AUTH_TYPE: N8N_AUTH_BASIC,
        CONF_USERNAME: _SECRETS["username"],
        CONF_PASSWORD: _SECRETS["password"],
        CONF_HEADER_NAME: _NON_SECRET_HEADER_NAME,
        CONF_HEADER_VALUE: _SECRETS["header_value"],
    },
}

# Secret keys expected to be redacted in each preset's parent data.
_REDACTED_KEYS: dict[str, set[str]] = {
    BACKEND_OPENAI_COMPAT: {CONF_BASE_URL, CONF_API_KEY},
    BACKEND_LANGGRAPH: {CONF_BASE_URL, CONF_API_KEY},
    BACKEND_OLLAMA: {CONF_BASE_URL, CONF_API_KEY},
    BACKEND_CONVERSE: {CONF_BASE_URL, CONF_TOKEN, CONF_URL},
    BACKEND_N8N: {
        CONF_WEBHOOK_URL,
        CONF_USERNAME,
        CONF_PASSWORD,
        CONF_HEADER_VALUE,
    },
}


def _build_entry(hass: HomeAssistant, backend_type: str) -> MockConfigEntry:
    """Parent entry for ``backend_type`` with one secret-laden conversation subentry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Middleman {backend_type}",
        version=2,
        data={CONF_BACKEND_TYPE: backend_type, **_PARENT_DATA[backend_type]},
        subentries_data=[
            ConfigSubentryData(
                data={
                    CONF_NAME: "Agent 0",
                    # Both prompt keys are live across the codebase; seed both.
                    CONF_PROMPT: _SECRETS["prompt"],
                    CONF_SYSTEM_PROMPT: _SECRETS["system_prompt"],
                    CONF_MODEL: _NON_SECRET_MODEL,
                },
                subentry_type="conversation",
                title="Agent 0",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(
    "backend_type",
    [
        BACKEND_OPENAI_COMPAT,
        BACKEND_LANGGRAPH,
        BACKEND_OLLAMA,
        BACKEND_CONVERSE,
        BACKEND_N8N,
    ],
)
async def test_diagnostics_redacts_every_secret_per_preset(hass: HomeAssistant, backend_type: str) -> None:
    """Every credential/URL/prompt is redacted and no raw secret leaks, per preset."""
    entry = _build_entry(hass, backend_type)

    result = await async_get_config_entry_diagnostics(hass, entry)

    # Every sensitive parent key is replaced by the sentinel.
    for key in _REDACTED_KEYS[backend_type]:
        assert result["data"][key] == REDACTED, f"{backend_type}:{key} not redacted"

    # Both prompt keys in the subentry are redacted.
    subentries = result["subentries"]
    assert len(subentries) == 1
    (subentry_dump,) = subentries.values()
    assert subentry_dump["data"][CONF_PROMPT] == REDACTED
    assert subentry_dump["data"][CONF_SYSTEM_PROMPT] == REDACTED

    # No raw secret string appears anywhere in the serialized dump.
    blob = json.dumps(result)
    for secret in _SECRETS.values():
        assert secret not in blob, f"{backend_type} leaked {secret!r}"

    # Non-sensitive structure survives, unredacted.
    assert result["backend_type"] == backend_type
    assert result["entry_id"] == entry.entry_id
    assert result["title"] == f"Middleman {backend_type}"
    assert result["entry_version"] == "2.1"
    assert subentry_dump["title"] == "Agent 0"
    assert subentry_dump["subentry_type"] == "conversation"
    assert subentry_dump["data"][CONF_NAME] == "Agent 0"
    assert subentry_dump["data"][CONF_MODEL] == _NON_SECRET_MODEL


async def test_diagnostics_includes_entities(hass: HomeAssistant) -> None:
    """A loaded entry's conversation entity appears in the dump's ``entities`` map."""
    entry = build_config_entry(hass, backend_type="fake", subentry_count=1)

    with patch.dict(
        "custom_components.llm_middleman.backends.BACKEND_TO_CLS",
        {"fake": FakeAdapter},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await async_get_config_entry_diagnostics(hass, entry)

    entities = result["entities"]
    assert len(entities) == 1
    (entity_id,) = entities
    assert entity_id.startswith("conversation.")


async def test_diagnostics_http_download_path(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,  # pyright: ignore[reportUnknownParameterType]
) -> None:
    """HA auto-discovers diagnostics.py and serves a redacted dump over HTTP."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Middleman http",
        version=2,
        data={
            CONF_BACKEND_TYPE: "fake",
            CONF_BASE_URL: _SECRETS["base_url"],
            CONF_API_KEY: _SECRETS["api_key"],
        },
        subentries_data=[
            ConfigSubentryData(
                data={CONF_NAME: "Agent 0", CONF_PROMPT: _SECRETS["prompt"]},
                subentry_type="conversation",
                title="Agent 0",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)

    with patch.dict(
        "custom_components.llm_middleman.backends.BACKEND_TO_CLS",
        {"fake": FakeAdapter},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    data_section = result["data"]
    assert isinstance(data_section, dict)
    assert data_section[CONF_API_KEY] == REDACTED
    assert data_section[CONF_BASE_URL] == REDACTED
    blob = json.dumps(result)
    assert _SECRETS["api_key"] not in blob
    assert _SECRETS["base_url"] not in blob
    assert _SECRETS["prompt"] not in blob
