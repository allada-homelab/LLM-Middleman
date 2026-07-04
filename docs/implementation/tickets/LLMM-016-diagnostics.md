---
id: LLMM-016
title: Diagnostics (redacted config-entry diagnostics)
status: in-review
phase: 4
depends_on: [LLMM-006, LLMM-007]
---

# LLMM-016 — Diagnostics (redacted config-entry diagnostics)

## Context

A config-entry diagnostics handler is a cheap HACS-quality win that lets the owner (and
bug reporters) download the integration's config state with secrets redacted. This
implements the `diagnostics.py` bullet in `plan.md` §Adjacent HA AI capabilities ("In
v1") and §Housekeeping.

Template: core `anthropic/diagnostics.py` in this repo's venv at
`.venv/lib/python3.14/site-packages/homeassistant/components/anthropic/diagnostics.py` —
it is the parent-entry + subentries shape this integration needs (v1 uses the
parent-entry + conversation-subentry model from LLMM-006/LLMM-007). Read it before
starting; the whole file is ~60 lines.

This depends on LLMM-006 (parent flow — defines the parent `entry.data` keys) and
LLMM-007 (subentry flow — defines the per-agent subentry `data` keys) so the redaction
set and the dumped shape match what actually exists.

## Scope

**In:**
- `custom_components/llm_middleman/diagnostics.py` with
  `async_get_config_entry_diagnostics(hass, entry)` returning the parent entry metadata,
  redacted parent `data`/`options`, redacted per-subentry `data`, and the entry's
  entities — mirroring the anthropic template.
- A `TO_REDACT` set covering every secret/PII field across **all five** backend presets'
  parent and subentry schemas (auth tokens/keys, URLs, prompts).

**Out:**
- Device or entity diagnostics (`async_get_device_diagnostics`) — not needed; the
  integration has one service device per subentry with no sensitive state.
- Any new logging or redaction in the request path (LLMM-005 already redacts auth from
  logs) — this ticket is the downloadable diagnostics dump only.
- `strings.json`/translation changes — diagnostics needs none.

## Implementation notes

**File shape (copy anthropic `diagnostics.py` structure):**
```python
async def async_get_config_entry_diagnostics(hass, entry):
    return {
        "title": entry.title,
        "entry_id": entry.entry_id,
        "entry_version": f"{entry.version}.{entry.minor_version}",
        "state": entry.state.value,
        "backend_type": entry.data.get(CONF_BACKEND_TYPE),   # useful, not sensitive
        "data": async_redact_data(entry.data, TO_REDACT),
        "options": async_redact_data(entry.options, TO_REDACT),
        "subentries": {
            se.subentry_id: {
                "title": se.title,
                "subentry_type": se.subentry_type,
                "data": async_redact_data(se.data, TO_REDACT),
            }
            for se in entry.subentries.values()
        },
        "entities": {
            ee.entity_id: ee.extended_dict
            for ee in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        },
    }
```
`async_redact_data` from `homeassistant.components.diagnostics`; `er` from
`homeassistant.helpers.entity_registry`.

**Sequencing.** A *complete* `TO_REDACT` needs the credential/URL `CONF_*` consts that the
adapter tickets add — `CONF_TOKEN` (converse), `CONF_WEBHOOK_URL` + the n8n auth fields,
LangGraph's deployment URL/`x-api-key`, etc. — which land in LLMM-008..012 (Phase 2).
Execute this ticket **after Phase 2 lands** so every preset's secret keys exist to redact.
The `depends_on` stays `[LLMM-006, LLMM-007]` (those define the shape); the per-preset leak
test in Verification is the enforcement that nothing was missed.

**`TO_REDACT` — enumerate against the actual LLMM-006/LLMM-007 `CONF_*` keys, not from
memory.** Cross-check `const.py` at build time. Per plan §Per-connector matrix the
sensitive parent/agent fields are:
- Auth: `CONF_API_KEY`, `CONF_TOKEN` (converse bearer), n8n auth credential fields
  (basic-auth password, custom-header value). Redact all of them.
- URLs: `CONF_URL` / `base_url`, n8n `webhook_url`, LangGraph deployment URL — redact
  (plan says "redact … URLs"; a webhook URL is effectively a secret, and base URLs leak
  LAN topology).
- Prompts: `CONF_SYSTEM_PROMPT` (`CONF_PROMPT`) — redact (may contain personal context).
Import HA's canonical constants where they exist (`CONF_API_KEY`, `CONF_URL` from
`homeassistant.const`) and the integration's own `CONF_*` from `.const` for the rest.
Redact conservatively: an over-redacted diagnostic is safe; a leaked token is not.

**Registration.** No manifest change is required — HA auto-discovers
`diagnostics.async_get_config_entry_diagnostics` by module presence. Confirm the
integration shows a "Download diagnostics" button on the entry.

## Acceptance criteria

- [x] `diagnostics.py` exists and exports `async_get_config_entry_diagnostics`.
- [x] The dump includes parent metadata, redacted parent `data`/`options`, every
      subentry's redacted `data`, and the entry's entities.
- [x] `TO_REDACT` covers every auth field, every URL/webhook field, and the system prompt
      across all five presets; a diagnostics dump for each preset shows `**REDACTED**` in
      place of those values and never a raw token/URL/prompt.
- [x] Gates green: `just check` + `just typecheck`.

## Verification

Write and run (`just test`) a unit test using the `pytest-homeassistant-custom-component`
diagnostics helper (`tests.components.diagnostics.get_diagnostics_for_config_entry`, per
core's own diagnostics tests) or a direct call:
1. Build a config entry + a conversation subentry for **each** preset with known dummy
   secrets (e.g. `api_key="SECRET"`, `webhook_url="https://n8n.local/webhook/x/chat"`,
   `system_prompt="my name is X"`).
2. Call the handler; assert the returned dict contains `**REDACTED**` for every field in
   `TO_REDACT` and that the literal secret strings appear **nowhere** in
   `json.dumps(result)`.
3. Assert non-sensitive fields (`backend_type`, `entry_id`, subentry `title`) are present
   and unredacted.

**Executed (2026-07-04, branch `llmm-016-diagnostics`):**

`tests/test_diagnostics.py` implements exactly this: a parametrized
`test_diagnostics_redacts_every_secret_per_preset` builds a v2 parent entry + one
conversation subentry for **each** of the five presets with dummy secrets
(`api_key="SECRET_API_KEY"`, `token="SECRET_TOKEN"`, `password="SECRET_PASSWORD"`,
`header_value="SECRET_HEADER_VALUE"`, `username="secret-user"`,
`base_url`/`webhook_url`/legacy `url`, and both `prompt`/`system_prompt`); it asserts every
sensitive parent key and both subentry prompt keys equal `**REDACTED**`, that no raw secret
literal survives in `json.dumps(result)`, and that non-sensitive fields (`backend_type`,
`entry_id`, `title`, subentry `title`/`name`/`model`) are present and unredacted.
`test_diagnostics_includes_entities` confirms a loaded entry's conversation entity appears
in the `entities` map, and `test_diagnostics_http_download_path` drives the real
`/api/diagnostics/config_entry/<id>` HTTP endpoint via the HA diagnostics helper, proving
HA auto-discovers `diagnostics.py` (no manifest change) and returns a redacted dump.

Gate results in this session:

- `just typecheck` → `0 errors, 0 warnings, 0 notes` (strict basedpyright).
- `just check` (lock-check + lint + fmt-check + test) → `196 passed, 2 warnings in 5.38s`
  (189 baseline + 7 new; the 2 warnings are the pre-existing aiohttp `BasicAuth`
  deprecation in the unrelated n8n test).

Manual (owner, optional): on the live HA instance, open the integration entry → overflow
menu → Download diagnostics; confirm the JSON has no plaintext token/URL/prompt. NOT run
(no live HA instance in this environment); the HTTP-path unit test above covers the same
download endpoint programmatically.

## Risks / open questions

- The redaction set is only correct if it tracks LLMM-006/LLMM-007's final `CONF_*` keys
  — re-grep `const.py` when those tickets land; a renamed/added secret key silently leaks
  if missed. The per-preset test in Verification is the guard against that.
- `entity_entry.extended_dict` shape is HA-version-dependent; if it ever includes
  sensitive attributes for this integration (it shouldn't — no sensitive entity state),
  redact them too. Verify against the pinned HA in the venv.
