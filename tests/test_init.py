"""Tests for the LLM Middleman entry setup/teardown (v1, LLMM-005)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState, ConfigSubentry
from homeassistant.const import CONF_PROMPT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_middleman import (
    async_migrate_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.llm_middleman import conversation as conversation_platform
from custom_components.llm_middleman.backends import BACKEND_TO_CLS
from custom_components.llm_middleman.backends.base import BackendAdapter, BackendAuthError, BackendConnectionError
from custom_components.llm_middleman.backends.converse import ConverseAdapter
from custom_components.llm_middleman.backends.dify import DifyAdapter
from custom_components.llm_middleman.backends.langgraph import LangGraphAdapter
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


class _ProbeFailingAdapter(FakeAdapter):
    """Fake adapter whose connection probe raises ``probe_error``."""

    probe_error: Exception = BackendConnectionError("down")

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        raise cls.probe_error


class _ProbeAuthFailingAdapter(_ProbeFailingAdapter):
    probe_error = BackendAuthError("bad key")


class _ProbeHangingAdapter(FakeAdapter):
    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        await asyncio.Event().wait()


async def _setup_with(hass: HomeAssistant, adapter_cls: type[BackendAdapter]) -> MockConfigEntry:
    entry = build_config_entry(hass, backend_type="fake")
    with patch.dict(_PATCH_FACTORY, {"fake": adapter_cls}):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_setup_retries_when_backend_unreachable(hass: HomeAssistant) -> None:
    """A failing connection probe defers setup (SETUP_RETRY) instead of loading a dead agent."""
    entry = await _setup_with(hass, _ProbeFailingAdapter)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_failure_starts_reauth(hass: HomeAssistant) -> None:
    """Rejected credentials raise ConfigEntryAuthFailed, so HA starts the reauth flow."""
    entry = await _setup_with(hass, _ProbeAuthFailingAdapter)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))) == 1


@pytest.mark.parametrize(
    ("adapter_cls", "exc_cls", "key"),
    [
        (_ProbeAuthFailingAdapter, ConfigEntryAuthFailed, "auth_failed"),
        (_ProbeFailingAdapter, ConfigEntryNotReady, "backend_not_ready"),
    ],
)
async def test_setup_failures_are_translated(
    hass: HomeAssistant, adapter_cls: type[BackendAdapter], exc_cls: type[Exception], key: str
) -> None:
    """Setup failures carry a translation key, so the UI shows a localized reason."""
    entry = build_config_entry(hass, backend_type="fake")

    with patch.dict(_PATCH_FACTORY, {"fake": adapter_cls}), pytest.raises(exc_cls) as raised:
        await async_setup_entry(hass, entry)

    assert isinstance(raised.value, HomeAssistantError)
    assert (raised.value.translation_domain, raised.value.translation_key) == (DOMAIN, key)


def _create_unreachable_issue(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    issue_id = f"backend_unreachable_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="backend_unreachable",
    )
    return issue_id


async def test_successful_setup_clears_the_unreachable_issue(hass: HomeAssistant) -> None:
    """A setup whose probe reached the backend proves it reachable, clearing the repair issue."""
    entry = build_config_entry(hass, backend_type="fake")
    issue_id = _create_unreachable_issue(hass, entry)

    with patch.dict(_PATCH_FACTORY, {"fake": FakeAdapter}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_removing_entry_clears_its_unreachable_issue(hass: HomeAssistant) -> None:
    """A removed connection leaves no orphaned repair issue behind."""
    entry = build_config_entry(hass, backend_type="fake")
    issue_id = _create_unreachable_issue(hass, entry)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_setup_retries_when_probe_hangs(hass: HomeAssistant) -> None:
    """A probe that never answers is bounded and treated as unreachable."""
    with patch("custom_components.llm_middleman.PROBE_TIMEOUT", 0.01):
        entry = await _setup_with(hass, _ProbeHangingAdapter)
    assert entry.state is ConfigEntryState.SETUP_RETRY


def test_conversation_platform_does_not_limit_parallel_turns() -> None:
    """PARALLEL_UPDATES = 0 (core convention): concurrent turns are never serialized."""
    assert conversation_platform.PARALLEL_UPDATES == 0


def test_manifest_loads_after_assist_pipeline_and_intent() -> None:
    """Mirror core conversation integrations' after_dependencies."""
    manifest = json.loads((Path(__file__).parents[1] / "custom_components/llm_middleman/manifest.json").read_text())
    assert manifest["after_dependencies"] == ["assist_pipeline", "intent"]


@pytest.mark.parametrize("adapter_cls", list(BACKEND_TO_CLS.values()))
def test_adapter_declares_every_store_it_creates(hass: HomeAssistant, adapter_cls: type[BackendAdapter]) -> None:
    """``store_keys`` names exactly the Stores an adapter builds, so entry removal can't miss one."""
    adapter = adapter_cls(hass, MagicMock(), {CONF_BASE_URL: "http://b", "entry_id": "entry-1"})
    created = {value.key for value in vars(adapter).values() if isinstance(value, Store)}

    assert created == set(adapter_cls.store_keys("entry-1"))


@pytest.mark.parametrize("adapter_cls", [DifyAdapter, LangGraphAdapter])
async def test_removing_entry_deletes_its_session_map_store(
    hass: HomeAssistant, hass_storage: dict[str, Any], adapter_cls: type[BackendAdapter]
) -> None:
    """Deleting a stateful entry removes every Store it persisted, leaving other entries' alone."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={CONF_BACKEND_TYPE: adapter_cls.backend_type})
    entry.add_to_hass(hass)
    own_keys = adapter_cls.store_keys(entry.entry_id)
    other_key = f"{DOMAIN}.{adapter_cls.backend_type}.other_entry"
    hass_storage.update({key: {"version": 1, "data": {"dev-1": "thread-1"}} for key in own_keys})
    hass_storage[other_key] = {"version": 1, "data": {"dev-2": "thread-2"}}

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not set(own_keys) & set(hass_storage)
    assert other_key in hass_storage


def test_langgraph_declares_its_pending_resume_store() -> None:
    """The interrupt->resume Store is declared alongside the thread map (regression)."""
    assert LangGraphAdapter.store_keys("e1") == (f"{DOMAIN}.langgraph.e1", f"{DOMAIN}.langgraph.e1.pending_resume")


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


async def test_runtime_subentry_add_creates_entity_without_reload(hass: HomeAssistant) -> None:
    """Adding a conversation subentry at runtime creates its entity live, no manual reload.

    Regression for BUG-1: without the entry update listener, the conversation platform's
    async_setup_entry only enumerates subentries at initial load, so an agent added after
    setup (the UI subentry flow's async_add_subentry) got no entity until the user reloaded
    the entry. The listener registered in async_setup_entry makes async_add_subentry trigger
    an automatic reload — this test never calls async_reload itself.
    """
    entry = build_config_entry(hass, backend_type="fake", subentry_count=0)
    registry = er.async_get(hass)

    with patch.dict(_PATCH_FACTORY, {"fake": FakeAdapter}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # No conversation subentries yet → no entities.
        assert _conversation_entity_ids(registry, entry) == set()

        # Add an agent at runtime exactly as the subentry-creation flow does. The
        # framework fires the entry's update listeners, which auto-reload the entry.
        subentry = ConfigSubentry(
            data=MappingProxyType({CONF_NAME: "Runtime Agent", CONF_PROMPT: ""}),
            subentry_type=SUBENTRY_TYPE_CONVERSATION,
            title="Runtime Agent",
            unique_id=None,
        )
        hass.config_entries.async_add_subentry(entry, subentry)
        await hass.async_block_till_done()

        # The entity exists WITHOUT any manual reload call in this test.
        assert _conversation_entity_ids(registry, entry) == {subentry.subentry_id}


def _conversation_entity_ids(registry: er.EntityRegistry, entry: MockConfigEntry) -> set[str]:
    """Return the unique_ids of the entry's conversation entities."""
    return {
        ent.unique_id
        for ent in registry.entities.values()
        if ent.config_entry_id == entry.entry_id and ent.domain == "conversation"
    }


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
    assert isinstance(moved, dr.DeviceEntry)
    assert moved.identifiers == {(DOMAIN, subentry.subentry_id)}
    assert subentry.subentry_id in _device_subentries(moved, entry.entry_id)


def _device_subentries(device: dr.DeviceEntry, entry_id: str) -> set[str | None]:
    """Return the device's subentries under ``entry_id`` on both device-registry models."""
    if hasattr(device, "config_subentry_id"):  # HA >= 2026.10: one entry + subentry per device
        return {device.config_subentry_id} if device.config_entry_id == entry_id else set()
    return device.config_entries_subentries.get(entry_id, set())


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

    # The v0 URL is not a live server; the setup probe is exercised by its own tests.
    with patch.object(ConverseAdapter, "async_validate_connection"):
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
