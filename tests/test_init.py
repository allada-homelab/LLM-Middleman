"""Tests for the LLM Middleman entry setup/teardown (v1, LLMM-005)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.llm_middleman import async_setup_entry, async_unload_entry
from custom_components.llm_middleman.backends.base import BackendAdapter

from .conftest import build_config_entry
from .test_conversation import FakeAdapter

_PATCH_FACTORY = "custom_components.llm_middleman.backends.BACKEND_TO_CLS"


async def test_setup_builds_adapter_and_forwards(hass: HomeAssistant) -> None:
    """Setup constructs the adapter via the factory and forwards CONVERSATION."""
    entry = build_config_entry(hass, backend_type="fake")

    with (
        patch.dict(_PATCH_FACTORY, {"fake": FakeAdapter}),
        patch.object(hass.config_entries, "async_forward_entry_setups") as forward,
    ):
        assert await async_setup_entry(hass, entry) is True

    assert isinstance(entry.runtime_data, FakeAdapter)
    assert isinstance(entry.runtime_data, BackendAdapter)
    assert entry.runtime_data.connection_data["backend_type"] == "fake"
    forward.assert_called_once()


async def test_unload_delegates(hass: HomeAssistant) -> None:
    """Unload delegates to async_unload_platforms and propagates its result."""
    entry = build_config_entry(hass, backend_type="fake")

    with patch.object(hass.config_entries, "async_unload_platforms", return_value=True):
        assert await async_unload_entry(hass, entry) is True
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=False):
        assert await async_unload_entry(hass, entry) is False


async def test_setup_unknown_backend_raises(hass: HomeAssistant) -> None:
    """An unregistered backend type raises rather than silently no-op."""
    entry = build_config_entry(hass, backend_type="does-not-exist")
    with pytest.raises(ValueError, match="does-not-exist"):
        await async_setup_entry(hass, entry)


async def test_setup_and_unload_end_to_end(hass: HomeAssistant) -> None:
    """A full setup/unload cycle through the config-entries manager is clean."""
    entry = build_config_entry(hass, backend_type="fake")

    with patch.dict(_PATCH_FACTORY, {"fake": FakeAdapter}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert isinstance(entry.runtime_data, FakeAdapter)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_one_entity_per_subentry(hass: HomeAssistant) -> None:
    """One conversation entity per subentry, each with a distinct unique_id."""
    entry = build_config_entry(hass, backend_type="fake", subentry_count=2)

    with patch.dict(_PATCH_FACTORY, {"fake": FakeAdapter}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    entities = [
        ent
        for ent in registry.entities.values()
        if ent.config_entry_id == entry.entry_id and ent.domain == "conversation"
    ]
    assert len(entities) == 2
    unique_ids = {ent.unique_id for ent in entities}
    assert unique_ids == {sub.subentry_id for sub in entry.subentries.values()}


async def test_zero_subentries_zero_entities(hass: HomeAssistant) -> None:
    """A parent entry with no conversation subentries creates no entities."""
    entry = build_config_entry(hass, backend_type="fake", subentry_count=0)

    with patch.dict(_PATCH_FACTORY, {"fake": FakeAdapter}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    entities = [
        ent
        for ent in registry.entities.values()
        if ent.config_entry_id == entry.entry_id and ent.domain == "conversation"
    ]
    assert entities == []
