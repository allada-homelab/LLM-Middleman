---
id: LLMM-013
title: v0→v1 config-entry migration (v0 entry → converse parent + subentry)
status: in-review
phase: 2
depends_on: [LLMM-007, LLMM-009]
---

# LLMM-013 — v0→v1 config-entry migration (v0 entry → converse parent + subentry)

## Context
Implements `plan.md §Config flow → Migration` and the `async_migrate_entry` bullet in
`§Package layout`. The v0 build is a single `ConfigEntry` (VERSION 1) with flat data
`{url, token, system_prompt, name}` and one conversation entity whose `unique_id` is
`entry.entry_id` (v0 `conversation.py:59`). v1 is a **parent entry + conversation subentry**
model where the entity's `unique_id` is the subentry id (LLMM-005). Without migration, an
owner who upgrades an installed v0 loses their entity and its automations/exposure.

This ticket converts a v0 entry in place into a **converse-type** parent (the v0
`/v1/converse` contract survives as the `converse` preset) plus one conversation subentry,
preserving entity-id continuity so the upgrade is seamless.

## Scope
**In:**
- `async_migrate_entry(hass, entry)` in `custom_components/llm_middleman/__init__.py`,
  handling `entry.version == 1 → 2`.
- Rewrite the flat v0 data into parent connection data:
  `{CONF_BACKEND_TYPE: BACKEND_CONVERSE, CONF_BASE_URL: <url>, CONF_TOKEN: <token>}`.
- Create exactly one `conversation` subentry carrying the agent options
  (`CONF_NAME` ← v0 `name`, `CONF_PROMPT` ← v0 `system_prompt`, `CONF_TIMEOUT` ← default).
- Re-point the existing conversation entity registry entry from `unique_id == entry.entry_id`
  to `unique_id == <new subentry_id>` and attach it (+ its device) to the subentry, so the
  entity_id is preserved.
- Set the entry to `version = 2`; return `True` (and `False`/leave-untouched for unknown
  future versions so a downgrade doesn't corrupt data).

**Out:** nothing else. (The `VERSION = 2` on the flow class is set by LLMM-006; the
`converse` adapter that makes the migrated entry functional is LLMM-009; the subentry
type/schema is LLMM-007. This ticket only performs the data migration.)

## Implementation notes
Files: add `async_migrate_entry` to `custom_components/llm_middleman/__init__.py`. Constants
from `const.py` (LLMM-003): `CONF_BACKEND_TYPE`, `BACKEND_CONVERSE`, `CONF_BASE_URL`,
`CONF_TOKEN`, `CONF_PROMPT`, `CONF_NAME`, `CONF_TIMEOUT`, `DEFAULT_TIMEOUT`, the
`"conversation"` subentry-type string. v0 keys to read: `CONF_URL` (`"url"`), `CONF_TOKEN`
(`"token"`), `CONF_SYSTEM_PROMPT` (`"system_prompt"`), `CONF_NAME` (`"name"`) — see v0
`const.py:6-9`.

**Template — copy the openai_conversation subentry migration** (it did exactly this in
2025.7: flat conversation entry → parent + `conversation` subentry, moving the entity +
device): `homeassistant/components/openai_conversation/__init__.py` (`async_migrate_entry` /
`async_migrate_integration`). **Verify the current API against the installed HA source**
before finalizing — the config-subentries API and registry helpers have churned.

Shape:
```python
from homeassistant.config_entries import ConfigSubentry, ConfigSubentryData
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import ulid as ulid_util

async def async_migrate_entry(hass, entry) -> bool:
    if entry.version > 2:
        return False                      # downgrade — refuse rather than corrupt
    if entry.version == 1:
        old = entry.data
        subentry_id = ulid_util.ulid_now()
        subentry = ConfigSubentry(
            data=MappingProxyType({
                CONF_NAME: old.get(CONF_NAME, "LLM Middleman"),
                CONF_PROMPT: old.get(CONF_SYSTEM_PROMPT),
                CONF_TIMEOUT: DEFAULT_TIMEOUT,
            }),
            subentry_type="conversation",
            title=old.get(CONF_NAME, "LLM Middleman"),
            unique_id=None,
            subentry_id=subentry_id,
        )
        hass.config_entries.async_update_entry(
            entry,
            data={
                CONF_BACKEND_TYPE: BACKEND_CONVERSE,
                CONF_BASE_URL: old[CONF_URL],
                CONF_TOKEN: old.get(CONF_TOKEN),
            },
            version=2,
            subentries=[subentry],          # or hass.config_entries.async_add_subentry(...)
        )
        # entity-id continuity: move the v0 entity onto the subentry + new unique_id
        ent_reg = er.async_get(hass)
        if (eid := ent_reg.async_get_entity_id("conversation", DOMAIN, entry.entry_id)):
            ent_reg.async_update_entity(
                eid, new_unique_id=subentry_id, config_subentry_id=subentry_id
            )
        # move the v0 device onto the subentry too (keeps device/entity links)
        dev_reg = dr.async_get(hass)
        if (dev := dev_reg.async_get_device(identifiers={(DOMAIN, entry.entry_id)})):
            dev_reg.async_update_device(
                dev.id, add_config_subentry_id=subentry_id, ...
            )
    return True
```
Confirm each helper's exact signature against the installed HA (`async_update_entry`
accepting `subentries=`, `ConfigSubentry`/`ConfigSubentryData` field names,
`async_update_entity(new_unique_id=…, config_subentry_id=…)`, and the device-move helper —
openai's migration is the authority). If `async_update_entry` doesn't take `subentries`,
use `hass.config_entries.async_add_subentry(entry, subentry)` after the data/version update.

**Why converse:** v0's `/v1/converse` contract is preserved as the `converse` preset
(`plan.md §Per-connector matrix → Custom /v1/converse`), so the migrated entry keeps talking
to the same backend with the same base URL + bearer token. `CONF_SYSTEM_PROMPT` maps to the
subentry `CONF_PROMPT` (the converse adapter forwards/uses it per LLMM-009's schema).

**Idempotency / safety:** the function must be safe to run once per version bump; guard on
`entry.version`. Do not delete the old flat keys destructively before the new structure is
committed — build the new data dict fresh and let `async_update_entry` replace `data` atomically.

## Acceptance criteria
- [x] A VERSION-1 entry with `{url, token, system_prompt, name}` migrates to VERSION 2 with
      `data == {CONF_BACKEND_TYPE: BACKEND_CONVERSE, CONF_BASE_URL: url, CONF_TOKEN: token}`.
- [x] Exactly one `conversation` subentry is created with the agent name (its `title`, the
      canonical location the add-agent subentry flow stores it), `CONF_PROMPT`
      (= v0 `system_prompt`), and `CONF_TIMEOUT`.
- [x] The pre-existing conversation entity's registry `unique_id` becomes the subentry id and
      it is attached to the subentry — the **entity_id is unchanged** across migration.
- [x] A future `version > 2` entry returns `False` (no corruption).
- [x] After migration the entry sets up cleanly (the `converse` adapter, LLMM-009, loads).
- [x] gates green: `just check` + `just typecheck`.

## Verification
Migration unit tests (`tests/test_init.py`, `pytest-homeassistant-custom-component`):
- `test_migrate_v0_entry` — build a `MockConfigEntry(version=1, data={"url":…,"token":…,
  "system_prompt":…,"name":…})`, register a conversation entity with `unique_id=entry_id`,
  run `hass.config_entries.async_setup(entry.entry_id)` (or call `async_migrate_entry`
  directly), then assert: `entry.version == 2`, parent `data` shape, one subentry with the
  mapped fields, and the entity registry entry now has `unique_id == subentry_id` and the
  same `entity_id` as before.
- `test_migrate_preserves_entity_id` — capture `entity_id` pre-migration; assert equal
  post-migration (continuity is the whole point).
- `test_migrate_missing_optional_fields` — v0 entry with no `token`/`system_prompt` migrates
  without KeyError (optionals default to None).
- `test_future_version_refused` — `version=3` entry ⇒ `async_migrate_entry` returns `False`.
Run `just test` + `just typecheck`; record the baseline failing set first, report the delta.
Manual (optional, owner's live HA per `plan.md §Verification` E2E): install v1 over an
existing v0 install and confirm the conversation entity keeps its entity_id and still talks
to the converse backend.

## Risks / open questions
- **Config-subentries API drift:** `ConfigSubentry` construction, whether
  `async_update_entry` accepts `subentries=`, and the exact registry-move helpers
  (`async_update_entity(new_unique_id=…, config_subentry_id=…)`, the device-move signature)
  MUST be verified against the installed HA source via the openai_conversation migration —
  do not trust the sketch above verbatim.
- **Entity-id continuity mechanism:** changing a registry `unique_id` is the supported way to
  keep the same `entity_id`; confirm HA allows the in-place `new_unique_id` change without a
  collision (there is no other entity on this unique_id, so it should be clean) and that the
  device move doesn't strand the entity.
- **Ordering vs adapter build:** `async_setup_entry` (LLMM-003/009) will build the converse
  adapter from the migrated `data`; ensure migration runs before setup (HA calls
  `async_migrate_entry` first) and that the migrated `data` matches exactly what the converse
  parent flow (LLMM-006) would have produced, so setup takes the same path.
