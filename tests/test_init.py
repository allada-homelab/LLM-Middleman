"""Tests for the LLM Middleman entry setup/teardown (v1, LLMM-005)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_PROMPT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_middleman import (
    async_migrate_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.llm_middleman.backends.base import BackendAdapter
from custom_components.llm_middleman.const import (
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

from .conftest import build_config_entry
from .test_conversation import FakeAdapter

_PATCH_FACTORY = "custom_components.llm_middleman.backends.BACKEND_TO_CLS"

_V0_DATA = {
    CONF_URL: "http://middleman.local:8000",
    CONF_TOKEN: "v0-secret",
    CONF_SYSTEM_PROMPT: "You are a helpful home assistant.",
    CONF_NAME: "Living Room",
}


def _build_v0_entry(hass: HomeAssistant, *, data: dict[str, object] | None = None) -> MockConfigEntry:
    """Add a v0 (VERSION 1) flat entry with a conversation entity keyed on entry_id."""
    entry = MockConfigEntry(domain=DOMAIN, title="Living Room", version=1, data=data or _V0_DATA)
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create("conversation", DOMAIN, entry.entry_id, config_entry=entry)
    return entry


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
    # __init__ injects entry_id so adapters can key per-entry storage.
    assert entry.runtime_data.connection_data["entry_id"] == entry.entry_id
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


async def test_migrate_v0_entry(hass: HomeAssistant) -> None:
    """A v0 flat entry becomes a converse parent + one conversation subentry."""
    entry = _build_v0_entry(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 2
    assert dict(entry.data) == {
        CONF_BACKEND_TYPE: BACKEND_CONVERSE,
        CONF_BASE_URL: _V0_DATA[CONF_URL],
        CONF_TOKEN: _V0_DATA[CONF_TOKEN],
    }

    assert len(entry.subentries) == 1
    subentry = next(iter(entry.subentries.values()))
    assert subentry.subentry_type == SUBENTRY_TYPE_CONVERSATION
    assert subentry.title == _V0_DATA[CONF_NAME]
    assert dict(subentry.data) == {
        CONF_PROMPT: _V0_DATA[CONF_SYSTEM_PROMPT],
        CONF_TIMEOUT: DEFAULT_TIMEOUT,
    }

    # The entity is re-keyed onto the subentry and attached to it.
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("conversation", DOMAIN, subentry.subentry_id)
    assert entity_id is not None
    assert registry.entities[entity_id].config_subentry_id == subentry.subentry_id
    # The old entry_id unique_id no longer resolves.
    assert registry.async_get_entity_id("conversation", DOMAIN, entry.entry_id) is None


async def test_migrate_preserves_entity_id(hass: HomeAssistant) -> None:
    """The conversation entity keeps its entity_id across migration (the whole point)."""
    entry = _build_v0_entry(hass)
    registry = er.async_get(hass)
    before = registry.async_get_entity_id("conversation", DOMAIN, entry.entry_id)
    assert before is not None

    assert await async_migrate_entry(hass, entry) is True

    subentry = next(iter(entry.subentries.values()))
    after = registry.async_get_entity_id("conversation", DOMAIN, subentry.subentry_id)
    assert after == before


async def test_migrate_moves_device_onto_subentry(hass: HomeAssistant) -> None:
    """A v0 device keyed on entry_id is re-identified and attached to the subentry."""
    entry = _build_v0_entry(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)})

    assert await async_migrate_entry(hass, entry) is True

    subentry = next(iter(entry.subentries.values()))
    moved = dev_reg.async_get(device.id)
    assert moved is not None
    assert moved.identifiers == {(DOMAIN, subentry.subentry_id)}
    assert subentry.subentry_id in moved.config_entries_subentries.get(entry.entry_id, set())


async def test_migrate_missing_optional_fields(hass: HomeAssistant) -> None:
    """A v0 entry lacking token/system_prompt migrates without KeyError (defaults None)."""
    entry = _build_v0_entry(hass, data={CONF_URL: _V0_DATA[CONF_URL]})

    assert await async_migrate_entry(hass, entry) is True

    assert dict(entry.data) == {
        CONF_BACKEND_TYPE: BACKEND_CONVERSE,
        CONF_BASE_URL: _V0_DATA[CONF_URL],
        CONF_TOKEN: None,
    }
    subentry = next(iter(entry.subentries.values()))
    assert subentry.data[CONF_PROMPT] is None
    assert subentry.title == "LLM Middleman"  # default when v0 carried no name


async def test_future_version_refused(hass: HomeAssistant) -> None:
    """A version > 2 entry is refused rather than corrupted (no downgrade)."""
    entry = MockConfigEntry(domain=DOMAIN, version=3, data={})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is False


async def test_migrated_entry_sets_up_end_to_end(hass: HomeAssistant) -> None:
    """A v0 entry migrates and then sets up green through the config-entries manager."""
    entry = _build_v0_entry(hass)
    registry = er.async_get(hass)
    before = registry.async_get_entity_id("conversation", DOMAIN, entry.entry_id)
    assert before is not None

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    assert entry.state is entry.state.LOADED
    assert entry.runtime_data.backend_type == BACKEND_CONVERSE

    # The migrated entity_id survives and the entity is live.
    subentry = next(iter(entry.subentries.values()))
    after = registry.async_get_entity_id("conversation", DOMAIN, subentry.subentry_id)
    assert after == before
    assert hass.states.get(before) is not None
