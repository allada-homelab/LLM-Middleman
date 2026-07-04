---
id: LLMM-006
title: Parent config flow (backend-type menu, per-backend connection steps, probes)
status: done
phase: 1
depends_on: [LLMM-003]
---

# LLMM-006 — Parent config flow (backend-type menu, per-backend connection steps, probes)

## Context
Implements the parent half of `plan.md §Config flow` and the Parent rows of
`§Per-connector configuration matrix`. The parent config entry holds one backend's
**connection + credentials**; per-agent tunables live in a conversation subentry (LLMM-007).
This flow copies the home-llm pattern (research-4): a single backend-type `SelectSelector`
step, then a per-backend connection form validated by that backend's adapter probe.

It fixes two v0 config-flow defects (`plan.md §Verified constraints`): URL-as-`unique_id`
(blocks two agents on one backend — duplicates are legitimate) and the base-URL reachability
probe that "validates nothing". Reference the v0 flow at
`custom_components/llm_middleman/config_flow.py` (its reconfigure shape at
`config_flow.py:74-89` is the template to keep; its `_async_check_reachable` and URL
`unique_id` are the parts being removed).

## Scope
**In:**
- `LLMMiddlemanConfigFlow(ConfigFlow, domain=DOMAIN)` with `VERSION = 2` (LLMM-013 owns the
  matching `async_migrate_entry`; setting the class `VERSION` here is what triggers it).
- Step 1 `async_step_user`: a `SelectSelector` over the `BACKEND_TO_CLS` keys (backend type),
  stored, then routed to the per-backend connection step.
- Per-backend connection steps building the **parent** fields from the matrix, with URL
  normalization (trailing-slash strip for base-URL backends; NOT for n8n's opaque webhook).
- Adapter-probe validation: call `BACKEND_TO_CLS[type].async_validate_connection(hass, data)`;
  map its raised exceptions to `errors["base"]`.
- **No URL/webhook `unique_id`** — do not call `async_set_unique_id`; duplicates are allowed.
- `async_step_reconfigure` re-running the same per-backend connection step + validation
  (backend type is fixed on reconfigure).
- The `config.step.*` / `config.error.*` / `config.abort.*` sections of `strings.json` +
  `translations/en.json` for the parent flow.

**Out:**
- The **subentry flow** (`async_get_supported_subentry_types`,
  `ConversationSubentryFlowHandler`, agent options) → **LLMM-007**, and its strings
  (`config_subentries.*` section).
- Each adapter's `async_validate_connection` **implementation** (the actual probe HTTP) →
  LLMM-008 (openai) / 009 (converse) / 010 (ollama) / 011 (langgraph) / 012 (n8n). This
  ticket only *calls* it and shapes the form/errors.

## Implementation notes
Files: rewrite `custom_components/llm_middleman/config_flow.py`; edit `strings.json` +
`translations/en.json`. Constants (`CONF_BACKEND_TYPE`, `BACKEND_OPENAI_COMPAT`, `BACKEND_LANGGRAPH`,
`BACKEND_OLLAMA`, `BACKEND_CONVERSE`, `BACKEND_N8N`, `CONF_BASE_URL`, `CONF_API_KEY`,
`CONF_TOKEN`, `CONF_ASSISTANT_ID`, `CONF_WEBHOOK_URL`, `CONF_TARGET_TYPE`, `CONF_AUTH_TYPE`,
etc.) and `BACKEND_TO_CLS` come from LLMM-003 (`const.py`, `backends/__init__.py`). Import
them; add any missing key to `const.py` and flag it in the PR.

**Pattern** (copy `homeassistant/components/openai_conversation/config_flow.py` for
multi-type shape; home-llm `custom_components/llama_conversation/config_flow.py` for the
SelectSelector-driven backend pick):
- Step 1 schema: `vol.Required(CONF_BACKEND_TYPE): SelectSelector(SelectSelectorConfig(
  options=[SelectOptionDict(value=k, label=k) for k in BACKEND_TO_CLS],
  translation_key="backend_type", mode=DROPDOWN))`. On submit, store
  `self._backend_type` and route: `return await getattr(self, f"async_step_{self._backend_type}")()`.
- One connection step per backend (`async_step_openai_compat`, `async_step_langgraph`,
  `async_step_ollama`, `async_step_converse`, `async_step_n8n`) — separate step_ids give
  clean per-backend translations (openai_conversation precedent). Each builds its schema,
  and on submit normalizes + validates + creates the entry.

**Per-backend parent fields** (matrix; every field is a real protocol setting):
- **openai_compat**: `CONF_BASE_URL` (URL, strip trailing slash), `CONF_API_KEY` (optional,
  password) — data_description must warn *many "OpenAI-compatible" servers require a
  non-empty dummy key*.
- **langgraph**: `CONF_BASE_URL` (deployment URL), `CONF_API_KEY` (optional, sent as
  `x-api-key`), `CONF_ASSISTANT_ID` (graph name/UUID, **default `"agent"`**).
- **ollama**: `CONF_BASE_URL` (host root, **not** `/v1`), `CONF_API_KEY` (optional).
- **converse**: `CONF_BASE_URL`, `CONF_TOKEN` (bearer) — the v0 contract's two fields.
- **n8n**: `CONF_WEBHOOK_URL` (full production chat URL `/webhook/<id>/chat`, pasted
  **verbatim — do NOT strip/normalize**, it is opaque), `CONF_TARGET_TYPE`
  (`SelectSelector`: Chat Trigger | plain Webhook), `CONF_AUTH_TYPE` (none | basic | custom
  header) + credential fields.

**URL normalization:** for base-URL backends do `data[CONF_BASE_URL] =
user_input[CONF_BASE_URL].rstrip("/")` **before** validation and storage (research-4 pitfall:
a trailing slash double-slashes `/v1//models` → 404). Leave `CONF_WEBHOOK_URL` untouched.

**Validation + create:**
```python
adapter_cls = BACKEND_TO_CLS[self._backend_type]
try:
    await adapter_cls.async_validate_connection(self.hass, data)
except BackendAuthError:
    errors["base"] = "invalid_auth"
except BackendConnectionError:
    errors["base"] = "cannot_connect"
else:
    return self.async_create_entry(title=<derived>, data={CONF_BACKEND_TYPE: type, **data})
```
(`BackendAuthError` is a subclass of `BackendConnectionError`, so catch it **first**.)
`async_validate_connection` raises typed errors (defined in `backends/base.py`, LLMM-003:
openai `GET /v1/models`, ollama `GET /api/tags`, langgraph `GET /ok` fallback
`POST /assistants/search`, converse transport check, n8n webhook probe). **Do NOT** re-add
v0's `_async_check_reachable` — the probe must hit the real endpoint. Title: a fixed
per-backend label (e.g. `"OpenAI-compatible"`) or `data[CONF_BASE_URL]`; the human-facing
agent name lives on the subentry, so the parent title need not be user-entered.

**No unique_id:** omit `async_set_unique_id` / `_abort_if_unique_id_configured` entirely —
two agents against one backend is a supported topology (plan §Config flow).

**Reconfigure:** `async_step_reconfigure` reads `self._get_reconfigure_entry()`, sets
`self._backend_type = entry.data[CONF_BACKEND_TYPE]` (type is immutable on reconfigure —
changing it would orphan subentries), and routes to the same per-backend step in a
reconfigure mode that ends in `async_update_reload_and_abort(entry, data=...)` (v0
`config_flow.py:74-89` shape). Prefill via `add_suggested_values_to_schema(schema, entry.data)`.

**strings.json:** add `config.step.user` (backend-type picker, with a `selector.backend_type`
translation block for the option labels), one `config.step.<backend>` block per connection
step (field labels + data_description including the dummy-key and host-root hints), and
`config.error.{cannot_connect,invalid_auth}` / `config.abort.reconfigure_successful`. Keep
the `config_subentries.*` section for LLMM-007.

## Acceptance criteria
- [x] Step 1 shows a backend-type dropdown sourced from `BACKEND_TO_CLS` keys; selecting one
      routes to that backend's connection step.
- [x] Each backend's connection form collects exactly its matrix Parent fields; base-URL
      backends strip a trailing slash, n8n's webhook URL is stored verbatim.
- [x] Submission calls `adapter_cls.async_validate_connection`; a raised
      `BackendConnectionError`/`BackendAuthError` re-shows the form with the mapped
      `errors["base"]` (`cannot_connect`/`invalid_auth`), no entry created.
- [x] A valid submission creates an entry whose `data` includes `CONF_BACKEND_TYPE`; no
      `unique_id` is set and a second identical URL is NOT aborted as duplicate.
- [x] `async_step_reconfigure` edits connection fields of an existing entry (type fixed) and
      ends in `async_update_reload_and_abort`.
- [x] `VERSION = 2` on the flow class.
- [x] `strings.json`/`translations/en.json` cover every parent step, field, error, and abort
      (88/88 keys match, verified by parity script). `hassfest` is not runnable in this
      environment (ships with the HA source tree, not the installed package) — NOT run;
      JSON well-formedness + strings/en key parity checked instead.
- [x] gates green: `just check` (lock-check + lint + fmt-check + test, 59 passed) + `just typecheck` (0 errors).

## Verification
Config-flow unit tests (`tests/test_config_flow.py`, `pytest-homeassistant-custom-component`
`hass` + `MockConfigEntry`), monkeypatching each adapter's `async_validate_connection`:
- `test_user_step_lists_backends` — result is a form with the `CONF_BACKEND_TYPE` selector.
- `test_full_flow_openai_creates_entry` — pick openai, submit base_url+key with the probe
  patched to succeed ⇒ `CREATE_ENTRY`, `data[CONF_BACKEND_TYPE]==BACKEND_OPENAI_COMPAT`,
  trailing slash stripped.
- `test_probe_failure_shows_error` — patch probe to raise `BackendConnectionError` ⇒ form
  re-shown with `errors["base"]=="cannot_connect"`, no entry (and `BackendAuthError` ⇒
  `"invalid_auth"`).
- `test_no_duplicate_abort` — create two entries with the same base_url ⇒ both created (no
  `already_configured` abort).
- `test_n8n_webhook_not_normalized` — a webhook URL with a trailing slash is stored verbatim.
- `test_reconfigure_updates_connection` — reconfigure an entry, patched probe succeeds ⇒
  `async_update_reload_and_abort`, type unchanged.
Run `just test` + `just typecheck`; record baseline first. Manually confirm translations:
`python -m script.hassfest` (or the repo's `just` recipe if present) reports no missing keys.

## Risks / open questions
- **Adapter probe contract (LLMM-003):** relies on `async_validate_connection(hass, data)`
  raising typed `BackendConnectionError`/`BackendAuthError` from `backends/base.py` (import
  them from there). The actual probe HTTP is each adapter ticket's job — this flow must
  degrade gracefully if only openai's probe exists in Phase 1 (other backends' steps can
  still validate once their adapters land).
- **Per-backend step methods vs one dynamic step:** separate step_ids are chosen for clean
  translations; if that bloats the flow, a single `async_step_connection` with a
  type-dispatched schema is the fallback (accept messier strings).
- **Title source:** parent title is a fixed backend label (agent name lives on the subentry);
  confirm this reads acceptably in the Settings › Devices & Services list before release.
