---
type: gotcha
title: Reconfigure/reauth abort must match whether the entry has an update listener
description: Reconfigure and reauth save via update_and_abort when the entry has an update listener, else update_reload_and_abort; reload+listener breaks in HA 2026.12.
tags: [config-flow, home-assistant]
generated: {by: okf-wiki/haiku, at: 2026-10-24T23:51:56Z}
verified:
  - {by: okf-wiki/haiku, at: 2026-10-24T23:51:56Z, commit: ad14312f1a7c}
  - {by: okf-wiki/deepseek, at: 2026-10-25T07:10:57Z, commit: e2b31f41a3ed}
sources:
  - {id: ha-blog-listener-reload, resource: https://contributors.home-assistant.io/blog/2026/05/07/config-entry-listener-together-with-reloading-methods/, note: "HA deprecation: a config flow using update_reload_and_abort with an update listener breaks in 2026.12"}
  - {id: ha-config-entries-log, resource: "HA core config_entries.py ~L3716-3722 (2026.10): logs breaks_in_ha_version=2026.12.0"}
  - {id: pr-97, resource: https://github.com/allada-homelab/LLM-Middleman/pull/97, note: Reconfigure fix}
  - {id: pr-100, resource: https://github.com/allada-homelab/LLM-Middleman/pull/100, note: Reauth and failed-setup reconfigure fix; test_reconfigure_reloads_entry_whose_setup_failed}
---

# Reconfigure/reauth abort must match whether the entry has an update listener

## Symptom

Saving a reconfigure or reauth form on a loaded entry triggers a second reload alongside the
listener's own reload, and HA logs a deprecation that breaks in 2026.12 [^ha-config-entries-log].

## What fails

Calling `async_update_reload_and_abort` unconditionally: correct only for an entry with no update
listener. Calling `async_update_and_abort` unconditionally: a failed-setup entry never registered
the listener, so nothing reloads and the new credentials stay unapplied.

## What works

`_async_update_entry_and_abort` in `config_flow.py` branches on `entry.update_listeners`: with a
listener, `async_update_and_abort` (the listener reloads once); without, `async_update_reload_and_abort`.
Both reconfigure and reauth route through it [^pr-97] [^pr-100].

## Why

HA deprecated reload-plus-listener in 2026.10 [^ha-blog-listener-reload]. `__init__.py` registers
`add_update_listener` in `async_setup_entry`, so only entries whose setup reached that point have a
listener; a failed setup never does.

## Verify

- `custom_components/llm_middleman/config_flow.py` :: `_async_update_entry_and_abort`
