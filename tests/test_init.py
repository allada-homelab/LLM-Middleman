"""Tests for the LLM Middleman entry setup/teardown."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_middleman import async_setup_entry, async_unload_entry
from custom_components.llm_middleman.const import CONF_URL, DOMAIN


async def test_setup_entry_creates_session_and_forwards(hass: HomeAssistant) -> None:
    """Setup stores an aiohttp session in runtime_data and forwards the platform."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: "http://middleman.local"})
    entry.add_to_hass(hass)

    mock_session = AsyncMock()
    with (
        patch(
            "custom_components.llm_middleman.async_create_clientsession",
            return_value=mock_session,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups") as mock_forward,
    ):
        result = await async_setup_entry(hass, entry)

    assert result is True
    assert entry.runtime_data is mock_session
    mock_forward.assert_called_once()


async def test_unload_entry_success(hass: HomeAssistant) -> None:
    """Unload delegates to async_unload_platforms."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: "http://middleman.local"})
    entry.add_to_hass(hass)
    entry.runtime_data = AsyncMock()

    with patch.object(hass.config_entries, "async_unload_platforms", return_value=True):
        assert await async_unload_entry(hass, entry) is True


async def test_unload_entry_failure(hass: HomeAssistant) -> None:
    """A failed platform unload is propagated as False."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: "http://middleman.local"})
    entry.add_to_hass(hass)
    entry.runtime_data = AsyncMock()

    with patch.object(hass.config_entries, "async_unload_platforms", return_value=False):
        assert await async_unload_entry(hass, entry) is False
