"""Test fixtures for LLM Middleman."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import chat_session
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_middleman.const import (
    CONF_NAME,
    CONF_TOKEN,
    CONF_URL,
    DOMAIN,
)

TEST_URL = "http://middleman.local:8000"
TEST_TOKEN = "test-token"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Automatically enable custom integrations in all tests."""


@pytest.fixture(autouse=True)
async def setup_ha_components(hass: HomeAssistant) -> None:
    """Set up required HA components for integration tests."""
    from homeassistant.setup import async_setup_component

    await async_setup_component(hass, "homeassistant", {})
    await hass.async_block_till_done()


@dataclass
class MockChatLog(conversation.ChatLog):
    """Mock ChatLog that allows controlling tool results (HA core test pattern)."""

    _mock_tool_results: dict = field(default_factory=dict)

    def mock_tool_results(self, results: dict) -> None:
        """Set mock tool results."""
        self._mock_tool_results = results

    @property
    def llm_api(self):
        """Return the LLM API."""
        return self._llm_api

    @llm_api.setter
    def llm_api(self, value):
        """Set LLM API."""
        self._llm_api = value


@pytest.fixture
async def mock_chat_log(hass: HomeAssistant) -> Generator[MockChatLog]:
    """Return a real (mocked-class) chat log within a chat session."""
    with (
        patch(
            "homeassistant.components.conversation.chat_log.ChatLog",
            MockChatLog,
        ),
        chat_session.async_get_chat_session(hass, "mock-conversation-id") as session,
        conversation.async_get_chat_log(hass, session) as chat_log,
    ):
        yield chat_log


@pytest.fixture
def mock_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a mock config entry added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Middleman",
        data={
            CONF_NAME: "Test Middleman",
            CONF_URL: TEST_URL,
            CONF_TOKEN: TEST_TOKEN,
        },
    )
    entry.add_to_hass(hass)
    return entry
