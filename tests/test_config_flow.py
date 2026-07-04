"""Tests for the LLM Middleman config flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.llm_middleman.const import (
    CONF_NAME,
    CONF_TOKEN,
    CONF_URL,
    DOMAIN,
)


async def test_user_flow_success(hass: HomeAssistant) -> None:
    """A reachable endpoint creates the config entry."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.llm_middleman.config_flow._async_check_reachable",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "My Agent",
                CONF_URL: "http://middleman.local:8000",
                CONF_TOKEN: "secret",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My Agent"
    assert result["data"][CONF_URL] == "http://middleman.local:8000"
    assert result["data"][CONF_TOKEN] == "secret"


async def test_user_flow_cannot_connect(hass: HomeAssistant) -> None:
    """An unreachable endpoint shows a cannot_connect error."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    with patch(
        "custom_components.llm_middleman.config_flow._async_check_reachable",
        return_value=False,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "My Agent", CONF_URL: "http://unreachable.local"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_duplicate_aborts(hass: HomeAssistant) -> None:
    """Re-adding the same endpoint aborts as already configured."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    existing = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_NAME: "Existing", CONF_URL: "http://middleman.local:8000"},
        unique_id="http://middleman.local:8000",
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.llm_middleman.config_flow._async_check_reachable",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Dup", CONF_URL: "http://middleman.local:8000/"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
