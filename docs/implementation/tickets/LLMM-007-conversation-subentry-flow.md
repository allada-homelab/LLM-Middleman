---
id: LLMM-007
title: Conversation subentry flow (agents: prompt/model/options)
status: done
phase: 1
depends_on: [LLMM-006]
---

# LLMM-007 — Conversation subentry flow (agents: prompt/model/options)

## Context
Implements the subentry half of `plan.md §Config flow` and the common Agent rows of
`§Per-connector configuration matrix`. Each conversation **agent** is a typed subentry under
a parent backend entry (LLMM-006), so one backend/credential can back several agents with
different prompts/models. This is the core 2025.3+ subentry pattern; openai_conversation
migrated to it in 2025.7 (research-1, research-4).

The subentry `data` produced here is exactly what the entity (LLMM-005) reads per agent.
Follows the simplest core template: `homeassistant/components/ollama/config_flow.py` (parent
= URL+key; one `ConfigSubentryFlow` whose `async_step_user` and `async_step_reconfigure`
both alias a shared `set_options` step).

## Scope
**In:**
- `@classmethod @callback async_get_supported_subentry_types(cls, config_entry)` on the
  parent `LLMMiddlemanConfigFlow` (LLMM-006) → `{"conversation": ConversationSubentryFlowHandler}`.
- `ConversationSubentryFlowHandler(ConfigSubentryFlow)` with
  `async_step_user = async_step_reconfigure = async_step_set_options` (ollama pattern),
  using post-2025.4 names (`self._get_entry()`, `self._entry_id`).
- The **common** agent subentry schema: agent name, system prompt (`TemplateSelector`),
  `CONF_MAX_HISTORY`, `CONF_TIMEOUT`, `CONF_MEMORY_SCOPE` (capability-gated), model dropdown
  (backends that support model selection), and the **reserved-but-hidden** `CONF_LLM_HASS_API`
  field.
- Model dropdown populated from the parent probe's model list where possible (openai
  `GET /v1/models`, ollama `GET /api/tags`), free-text fallback.
- The `config_subentries.conversation.*` section of `strings.json` + `translations/en.json`.

**Out:**
- **Backend-specific** agent tunables beyond the common set — openai `temperature`/`top_p`/
  `max_tokens` → LLMM-008; ollama `num_ctx`/`keep_alive`/`think` → LLMM-010; langgraph
  `input_messages_key`/`response_node_filter`/`stateless_runs` → LLMM-011; n8n `streaming`/
  `input_field`/`output_field`/`session_field` → LLMM-012. Each adapter ticket extends this
  handler's schema with its own fields; this ticket builds the framework + common fields.
- **Rendering** the `CONF_LLM_HASS_API` selector and wiring the tool loop → LLMM-014 (see
  the gating note below).
- `ai_task_data` subentry type → fast-follow, not v1.
- The parent connection flow + its strings → LLMM-006.

## Implementation notes
Files: add `ConversationSubentryFlowHandler` + `async_get_supported_subentry_types` to
`custom_components/llm_middleman/config_flow.py`; edit `strings.json` +
`translations/en.json`. Constants (`CONF_PROMPT`, `CONF_MAX_HISTORY`, `DEFAULT_MAX_HISTORY`,
`CONF_TIMEOUT`, `DEFAULT_TIMEOUT=60`, `CONF_MEMORY_SCOPE`, `MEMORY_SCOPE_CONVERSATION|DEVICE|
AGENT`, `CONF_MODEL`) from `const.py` (LLMM-001/003); `CONF_LLM_HASS_API` and
`llm.DEFAULT_INSTRUCTIONS_PROMPT` from `homeassistant.const`/`homeassistant.helpers.llm`.

**Handler shape** (copy `homeassistant/components/ollama/config_flow.py`
`OllamaSubentryFlowHandler`):
```python
class ConversationSubentryFlowHandler(ConfigSubentryFlow):
    async def async_step_set_options(self, user_input=None):
        entry = self._get_entry()                 # parent config entry (post-2025.4 name)
        adapter_cls = BACKEND_TO_CLS[entry.data[CONF_BACKEND_TYPE]]
        if user_input is not None:
            # create (async_step_user) vs reconfigure (async_step_reconfigure):
            if self.source == SOURCE_RECONFIGURE:
                return self.async_update_and_abort(entry, self._get_reconfigure_subentry(), data=user_input)
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        return self.async_show_form(step_id="set_options",
                                    data_schema=self._build_schema(adapter_cls, entry))
    async_step_user = async_step_set_options
    async_step_reconfigure = async_step_set_options
```
Confirm the exact create-vs-reconfigure return calls against the installed HA
`ConfigSubentryFlow` API (the ollama handler is the ground-truth reference — mirror whatever
it does).

**Common schema (`_build_schema`)** — every field is per-agent (matrix Agent rows):
- `vol.Required(CONF_NAME, default="…")` — the agent's display name.
- `vol.Optional(CONF_PROMPT)`: `TemplateSelector()`, suggested value
  `llm.DEFAULT_INSTRUCTIONS_PROMPT`. This is the per-agent system prompt (converse/n8n also
  accept it as an extra body field; langgraph prepends it when the graph uses MessagesState).
- `CONF_MAX_HISTORY`: `NumberSelector` (box, min 0), default `DEFAULT_MAX_HISTORY`. Controls
  the stateless-replay trim (openai full-replay ignores/uses it; ollama trims to
  `2*max_history+1`). Only meaningful for stateless backends — gate with
  `adapter_cls.supports_max_history` if 003 exposes it, else always show (harmless).
- `CONF_TIMEOUT`: `NumberSelector` (min 10, max 300), default `DEFAULT_TIMEOUT` (n8n adapter
  overrides the default to 30 in its schema extension).
- `CONF_MEMORY_SCOPE`: `SelectSelector` over `conversation`/`device`/`agent`, default
  `conversation`, **only when `adapter_cls.supports_memory_scope`** (stateful backends —
  plan §Conversation continuity "honest limit": stateless backends can't resurrect history
  HA discarded, so the option is hidden for them).
- `CONF_MODEL`: **only when the backend has a model catalog** — i.e. when
  `await adapter_cls.async_list_models(hass, entry.data)` returns a non-`None` list
  (openai/ollama; the base default is `None`, so converse/langgraph/n8n get no dropdown).
  Build a `SelectSelector(SelectSelectorConfig(options=<probe models>, custom_value=True,
  mode=DROPDOWN))` so it's a dropdown with free-text fallback; on any probe failure, degrade
  to a plain `TextSelector`. `async_list_models` is defined on the ABC (LLMM-003).

**`CONF_LLM_HASS_API` — reserved, hidden until LLMM-014** (task-critical coordination):
define/import the key and account for it in the schema builder, but **do not render the
selector yet** — it is only meaningful once the tool loop exists. Gate it on
`adapter_cls.supports_ha_tools AND <tool-loop-enabled>`; until LLMM-014 the second condition
is `False`, so the field is omitted. Leave a `# TODO(LLMM-014): render LLM_API_ASSIST
multi-select selector here when supports_ha_tools` at the exact site so it is visibly
incomplete (Anti-Potemkin rule). When 014 lands it flips the gate and adds the
`vol.Optional(CONF_LLM_HASS_API): SelectSelector(...multiple=True...)` over
`llm.async_get_apis(hass)` (list form → MCP tools free). Do NOT store a default value for it
now (an empty/omitted key must mean "no tools").

**Reconfigure vs create:** `async_step_user` is a *new* agent (`async_create_entry`);
`async_step_reconfigure` edits an existing subentry (`async_update_and_abort` /
`async_update_reload_and_abort` per the ollama reference). Prefill the reconfigure schema
from the existing subentry data via `add_suggested_values_to_schema`.

**strings.json:** add `config_subentries.conversation.step.set_options` (title +
`data`/`data_description` for name, prompt, model, max_history, timeout, memory_scope), the
`config_subentries.conversation.initiate_flow` labels (`user`/`reconfigure` entry-point
titles), and a `selector.memory_scope` translation block for the three scope labels. Leave
`CONF_LLM_HASS_API` strings for LLMM-014.

## Acceptance criteria
- [x] `async_get_supported_subentry_types` returns `{"conversation":
      ConversationSubentryFlowHandler}`; the "Add conversation agent" flow appears under a
      configured parent entry.
- [x] The set_options form shows name, prompt (TemplateSelector), max_history, and timeout
      for every backend; creating it produces a subentry whose `data` carries those keys.
- [x] `CONF_MODEL` appears as a dropdown-with-free-text only for model-capable backends
      (openai/ollama), populated from the probe when reachable, plain text on probe failure.
- [x] `CONF_MEMORY_SCOPE` appears only when `adapter_cls.supports_memory_scope`; absent for
      openai/ollama.
- [x] `CONF_LLM_HASS_API` is NOT rendered (hidden until LLMM-014) and no default is stored,
      with a `TODO(LLMM-014)` marker at the gate site.
- [x] `async_step_reconfigure` edits an existing agent's options and prefills current values.
- [x] `config_subentries.conversation.*` strings/translations complete; translations lint
      clean.
- [x] gates green: `just check` + `just typecheck`.

## Verification
Subentry-flow unit tests (`tests/test_config_flow.py`, `hass` + a `MockConfigEntry` parent
with `data[CONF_BACKEND_TYPE]` set), patching the model-list probe:
- `test_supported_subentry_types` — `LLMMiddlemanConfigFlow.async_get_supported_subentry_types`
  contains `"conversation"`.
- `test_create_conversation_subentry` — run the subentry `user` step, submit name+prompt+
  timeout ⇒ `CREATE_ENTRY` subentry with those keys.
- `test_model_dropdown_openai` — parent type openai, probe returns `["m1","m2"]` ⇒ schema
  offers those with `custom_value=True`; probe raises ⇒ falls back to text.
- `test_memory_scope_gated` — parent type openai (stateless, `supports_memory_scope=False`)
  ⇒ no `CONF_MEMORY_SCOPE` in schema; a stateful stub backend ⇒ present.
- `test_llm_hass_api_hidden` — `CONF_LLM_HASS_API` absent from the rendered schema for all
  backends (gate off pre-014).
- `test_reconfigure_prefills` — reconfigure an existing subentry ⇒ form suggested values
  match stored data.
Run `just test` + `just typecheck`; baseline first. Confirm strings via hassfest/translations
check.

## Risks / open questions
- **`ConfigSubentryFlow` API surface:** the exact create/reconfigure return methods
  (`async_create_entry` vs `async_update_and_abort` and how the reconfigure subentry is
  fetched) must be mirrored from the installed `homeassistant/components/ollama/config_flow.py`
  — do not code from memory (research-1 flags the 2025.3 rename churn).
- **Model-list probe timing:** listing models during the subentry flow re-hits the backend;
  if it is slow/unreachable the form must still render (free-text fallback).
  `async_list_models` is guaranteed by the ABC (LLMM-003).
- **`CONF_LLM_HASS_API` gate:** keeping the key defined-but-unrendered is deliberate
  (LLMM-014 flips it). Ensure no stored default leaks a truthy value that would silently
  enable tools before the loop exists.
