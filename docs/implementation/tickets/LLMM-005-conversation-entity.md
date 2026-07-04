---
id: LLMM-005
title: Backend-agnostic conversation entity (never-hangs guard, timeouts, continue_conversation, memory_scope)
status: in-review
phase: 1
depends_on: [LLMM-003, LLMM-004]
---

# LLMM-005 — Backend-agnostic conversation entity (never-hangs guard, timeouts, continue_conversation, memory_scope)

## Context
Implements `plan.md §Conversation entity (backend-agnostic, conversation.py)`,
`§Follow-up listening`, `§Conversation continuity (thread identity)`, and the never-hangs
parts of `§Streaming parsers`. This is the ONE `ConversationEntity` every backend shares:
it wires HA's ChatLog to an adapter's `stream_turn`, hardens v0's "never hang the pipeline"
guarantee, and applies per-agent timeouts, follow-up-listening, and session-key derivation.
All provider-specific streaming lives in the adapters (Phase 1/2), not here.

The v0 entity (`custom_components/llm_middleman/conversation.py`) is reference material —
its `_stream_deltas` fallback logic (`conversation.py:130-211`) is the seed of the guard,
and its agent-registration lifecycle (`conversation.py:72-80`) is ported here.

## Scope
**In:**
- Rewrite `custom_components/llm_middleman/__init__.py`: `async_setup_entry` builds the
  adapter via the LLMM-003 factory (`BACKEND_TO_CLS[entry.data[CONF_BACKEND_TYPE]]`),
  constructs it as `adapter_cls(hass, async_create_clientsession(hass), entry.data)`, stores
  it in `entry.runtime_data`, and forwards to the `CONVERSATION` platform;
  `async_unload_entry` unloads platforms. (LLMM-003 defines the constructor contract
  `__init__(hass, session, connection_data)`.)
- `LLMMiddlemanConversationEntity` in `custom_components/llm_middleman/conversation.py`,
  rebuilt backend-agnostic: reads the adapter from `config_entry.runtime_data` and its
  per-agent options from the **conversation subentry** it belongs to.
- `async_setup_entry` that adds **one entity per `conversation` subentry** (core
  openai/ollama pattern), not one per config entry.
- `_async_handle_message` chain: `async_provide_llm_data` → drive ONE `adapter.stream_turn`
  through the guard into `chat_log.async_add_delta_content_stream` → assemble
  `ConversationResult`.
- `_guarded()` never-hangs wrapper around the adapter stream: broad `except Exception`,
  `_LOGGER.exception`, role-first-delta enforcement, guaranteed ≥1 `AssistantContent` on
  every exit path (silent end, exception-before-content, exception-after-content).
- Per-agent timeout wiring: a shared `build_client_timeout(options)` helper producing
  `aiohttp.ClientTimeout(total=CONF_TIMEOUT, sock_read=IDLE_TIMEOUT)` that adapters use for
  their POST; the guard converts the resulting `TimeoutError` into the fallback.
- `continue_conversation` explicit override: build the result from
  `async_get_result_from_chat_log`, then OR-in `ctx.continue_conversation` (the adapter sets
  it on the per-turn `TurnContext`).
- `memory_scope` session-key derivation (`conversation`/`device`/`agent`), passed to the
  adapter via `ctx.memory_key` for stateful backends; capability-gated.
- Agent-registration lifecycle ported/modernized from v0 `conversation.py:72-80`.

**Out:**
- The multi-iteration **tool loop** (`for _ in range(MAX_TOOL_ITERATIONS)`,
  `chat_log.unresponded_tool_results`, `CONF_LLM_HASS_API` wiring,
  `ConversationEntityFeature.CONTROL`) → **LLMM-014**. This ticket drives `stream_turn`
  exactly once (text-only) and MUST factor that drive into a reusable method so 014 can
  wrap it in the loop. Pass `llm_api=None` to `async_provide_llm_data` for now.
- Any **adapter** implementation (`stream_turn`, `async_validate_connection`, SSE/NDJSON
  parsing) → LLMM-008/009/010/011/012.
- The **subentry schema/flow** that produces the options this entity reads → LLMM-007.
  (This entity only *consumes* `subentry.data`.)

## Implementation notes
Files: rewrite `custom_components/llm_middleman/__init__.py` and
`custom_components/llm_middleman/conversation.py`. Constants
(`CONF_TIMEOUT`, `DEFAULT_TIMEOUT=60`, `IDLE_TIMEOUT≈30`, `CONF_MEMORY_SCOPE`,
`MEMORY_SCOPE_CONVERSATION|DEVICE|AGENT`, `CONF_PROMPT`, the `"conversation"` subentry-type
string, `ERROR_MESSAGE`) come from `const.py` (owned by LLMM-001/003) — import them; if a
name is missing, add it to `const.py` and note it in the PR. The `BackendAdapter` ABC,
`TurnContext`, and the capability flags come from `backends/base.py` (LLMM-003).

**Entry setup (`__init__.py`):**
```python
async def async_setup_entry(hass, entry):
    adapter_cls = BACKEND_TO_CLS[entry.data[CONF_BACKEND_TYPE]]
    entry.runtime_data = adapter_cls(hass, async_create_clientsession(hass), entry.data)
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.CONVERSATION])
    return True

async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, [Platform.CONVERSATION])
```
(`async_create_clientsession` from `homeassistant.helpers.aiohttp_client`; the constructor
signature is the LLMM-003 contract.) Type `entry.runtime_data` as `BackendAdapter`.

**Per-subentry setup** (copy core `homeassistant/components/openai_conversation/__init__.py`
+ `entity.py::async_setup_entry`):
```python
async def async_setup_entry(hass, config_entry, async_add_entities):
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != "conversation":
            continue
        async_add_entities(
            [LLMMiddlemanConversationEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )
```
Entity `_attr_unique_id = subentry.subentry_id` (NOT `entry.entry_id` — that was v0; the
change is what LLMM-013 migrates). `_attr_has_entity_name = True`, `_attr_name = None`,
`_attr_supports_streaming = True`. `DeviceInfo` keyed on `(DOMAIN, subentry.subentry_id)`.
Store `self.entry`, `self.subentry`; adapter = `config_entry.runtime_data`; options =
`subentry.data`.

**`_async_handle_message`** (plan §Conversation entity, steps 1–3):
1. `try: await chat_log.async_provide_llm_data(user_input.as_llm_context(DOMAIN), None,
   options.get(CONF_PROMPT), user_input.extra_system_prompt)` — on
   `conversation.ConverseError` return `err.as_conversation_result()`. `llm_api` is `None`
   until LLMM-014. Forwarding `extra_system_prompt` here is also what makes
   `assist_satellite.start_conversation` work for free (plan §Adjacent HA — In v1).
2. Drive one turn (factored method, e.g. `_async_run_turn`): build the per-turn `TurnContext`,
   then
   ```python
   ctx = TurnContext(
       options=self.subentry.data,
       memory_key=self._derive_memory_key(user_input, chat_log),
   )
   async for _ in chat_log.async_add_delta_content_stream(
       self.entity_id,
       self._guarded(self.adapter.stream_turn(chat_log, user_input, ctx)),
   ):
       pass
   ```
   The `ctx` is created fresh per turn (never stored on the shared adapter — see LLMM-003 race
   rationale); the adapter reads `ctx.memory_key`/`ctx.options` and may set
   `ctx.continue_conversation`. Return `ctx` (or keep it in scope) so step 3 can read it.
3. `result = conversation.async_get_result_from_chat_log(user_input, chat_log)`; apply the
   continue-conversation override (below) using `ctx.continue_conversation`; `return result`.

**`_guarded()`** (harden v0 `conversation.py:130-211`; plan §Never-hangs guard). An async
generator wrapping the adapter stream:
- Track `started` (has any block been opened). For the **first** yielded delta: if it lacks
  a `"role"` key, emit `{"role": "assistant"}` before it (role-first invariant — HA's chat
  log rejects a first delta without a role; see research-4 "first yielded delta of each
  block MUST carry the role"). Set `started`.
- Pass every delta through **untrimmed** — empty-string `content` deltas included (do NOT
  `if delta.get("content")`-filter).
- `except Exception:` (broad — the v0 holes were `ValueError` from aiohttp's 64 KB readline
  limit and `UnicodeDecodeError`, which escaped v0's `except (TimeoutError, ClientError)`).
  `_LOGGER.exception(...)`; if not `started` emit `{"role": "assistant"}`; then emit
  `{"content": ERROR_MESSAGE}`. Return.
- After the loop with nothing yielded (silent end / done-with-no-delta): emit
  `{"role": "assistant"}` + `{"content": ERROR_MESSAGE}`.
This guarantees ≥1 `AssistantContent` on every path, so
`async_get_result_from_chat_log` never raises and the pipeline never hangs.

**Timeouts** (plan §Timeouts): add module-level (or import from a shared util)
```python
def build_client_timeout(options) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(
        total=options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT), sock_read=IDLE_TIMEOUT
    )
```
Adapters call this for their POST so a responsive-but-slow stream isn't killed by the
single total deadline (v0's 60 s bug). `CONF_TIMEOUT` is per-agent (default 60, range
10–300, from the subentry). The guard turns any `TimeoutError` into the fallback. If
LLMM-003's `base.py` already exposes a home for this helper, put it there instead and note
it in the PR.

**Follow-up listening** (plan §Follow-up listening): automatic detection (reply ending in
`?`) is already computed by HA into `result.continue_conversation`. Add the **explicit
override**: after building `result`, `result.continue_conversation = result.continue_conversation
or ctx.continue_conversation`. `ctx.continue_conversation` is a field on the per-turn
`TurnContext` (default `False`); the adapter sets it during `stream_turn` (converse from
`done.continue_conversation`, n8n from a `continueConversation` output field). Because a fresh
`TurnContext` is created per turn, there is no reset step and no cross-turn/cross-subentry
race. This channel is defined by the `BackendAdapter`/`TurnContext` contract (LLMM-003).

**memory_scope** (plan §Conversation continuity): derive the session key from the per-agent
`CONF_MEMORY_SCOPE` option and pass it as `ctx.memory_key`, which stateful adapters read
(stateless openai/ollama ignore it — they replay ChatLog):
- `conversation` (default) → key = `chat_log.conversation_id`.
- `device` → stable key derived from `user_input.device_id`; **fall back to
  `conversation_id` when `device_id` is None**.
- `agent` → one global key per agent = `self.subentry.subentry_id`.
The `memory_scope` option only appears in the subentry schema when
`adapter.supports_memory_scope` (LLMM-007 gates it); when absent the derivation defaults to
`conversation` scope. Helper: `_derive_memory_key(user_input, chat_log) -> str`.

**Agent lifecycle** (port v0 `conversation.py:72-80`): keep `async_added_to_hass` /
`async_will_remove_from_hass`. **Verify against the installed HA source** whether the
modern `ConversationEntity` (as used by core openai/ollama, added via the entity platform
with `config_subentry_id`) still needs the manual `conversation.async_set_agent` /
`async_unset_agent` calls and the `AbstractConversationAgent` base — this is a plan
implementation-time checkpoint (`plan.md §Implementation-time checkpoints`, first bullet).
Core openai_conversation registers via the platform and does NOT call `async_set_agent`;
prefer that if confirmed, else keep the v0 calls keyed on the subentry.

## Acceptance criteria
- [x] `async_setup_entry` builds the adapter via `BACKEND_TO_CLS[entry.data[CONF_BACKEND_TYPE]]
      (hass, async_create_clientsession(hass), entry.data)`, stores it in `entry.runtime_data`,
      and forwards the `CONVERSATION` platform; `async_unload_entry` unloads it. A config entry
      with a (fake) registered backend type sets up and unloads cleanly.
      (Uses the `get_backend_cls()` factory accessor — the LLMM-003 public wrapper over
      `BACKEND_TO_CLS` — so an unknown type raises a clear `ValueError` instead of a bare
      `KeyError`. `test_setup_builds_adapter_and_forwards`, `test_setup_and_unload_end_to_end`,
      `test_setup_unknown_backend_raises`, `test_unload_delegates`.)
- [x] One conversation entity is created per `conversation` subentry (unique_id =
      `subentry.subentry_id`); zero subentries ⇒ zero entities.
      (`test_one_entity_per_subentry`, `test_zero_subentries_zero_entities`.)
- [x] `_async_handle_message` calls `async_provide_llm_data` with `llm_api=None`, the
      subentry system prompt, and `extra_system_prompt`; `ConverseError` returns
      `err.as_conversation_result()`.
      (`test_provides_llm_data_without_ha_tools`, `test_converse_error_is_returned`.)
- [x] A successful turn streams the adapter's deltas into `chat_log` and returns a result
      whose response text equals the concatenated deltas. (`test_streams_deltas_and_builds_result`.)
- [x] The guard guarantees ≥1 `AssistantContent` for: (a) adapter yields nothing,
      (b) adapter raises `ValueError`/`UnicodeDecodeError`/`TimeoutError` before any delta,
      (c) adapter raises after ≥1 delta. No exception escapes `_async_handle_message`.
      (`test_guard_silent_end`, `test_guard_exception_before_content` [parametrized],
      `test_guard_exception_after_content`.)
- [x] The single-turn drive is factored into a method LLMM-014 can wrap in a loop (no
      inline `range(MAX_TOOL_ITERATIONS)` here). (`_async_run_turn`.)
- [x] `build_client_timeout(options)` returns `total=CONF_TIMEOUT`, `sock_read=IDLE_TIMEOUT`.
      (Lives in `backends/base.py`, not the entity module, so future adapters import it
      without a cycle back through `conversation`. `test_build_client_timeout_defaults`,
      `test_build_client_timeout_per_agent`.)
- [x] `result.continue_conversation` is `True` when either HA's `?`-detection OR
      `ctx.continue_conversation` (set by the adapter during `stream_turn`) is set.
      (`test_continue_conversation_override`, `test_continue_conversation_default_false`.)
- [x] `_derive_memory_key` returns `conversation_id` for `conversation` scope,
      device-derived key for `device` scope (falling back to `conversation_id` with no
      device), and `subentry_id` for `agent` scope. (`test_memory_key_scopes` [parametrized].)
- [x] gates green: `just check` + `just typecheck`.

## Verification
Unit tests in `tests/` using the ported MockChatLog harness (LLMM-004) and a **fake
adapter** whose `stream_turn` yields scripted deltas / raises scripted exceptions (do not
touch a real backend here — that is the adapter tickets' job):
- `test_streams_deltas_and_builds_result` — fake yields `{"role":"assistant"}`,
  `{"content":"Hello "}`, `{"content":"world"}`; assert chat log + result text ==
  "Hello world".
- `test_guard_silent_end` — fake yields nothing; assert one assistant message ==
  `ERROR_MESSAGE`, no raise.
- `test_guard_exception_before_content` — parametrize fake raising `ValueError`,
  `UnicodeDecodeError`, `TimeoutError`; assert `ERROR_MESSAGE`, `_LOGGER.exception` called.
- `test_guard_exception_after_content` — fake yields role+"partial" then raises; assert the
  partial content survives AND a fallback message is appended; no raise.
- `test_guard_injects_role_first` — fake first-yields `{"content":"x"}` with no role; assert
  a leading `{"role":"assistant"}` was injected (turn accepted, result text == "x").
- `test_empty_delta_passes_untrimmed` — fake yields `{"content":""}` then `{"content":"a"}`;
  assert the empty delta is not dropped.
- `test_continue_conversation_override` — the fake's `stream_turn` sets
  `ctx.continue_conversation = True` (after a non-`?` reply) ⇒ `result.continue_conversation
  is True`.
- `test_memory_key_scopes` — parametrize `conversation`/`device`(+with/without device_id)/
  `agent`; the fake records the `ctx` it received — assert `ctx.memory_key` equals the
  expected derived key.
- `test_one_entity_per_subentry` — config entry with 2 conversation subentries ⇒ 2 entities
  with distinct unique_ids.
- `test_setup_and_unload` — a config entry whose `backend_type` maps to a fake registered
  adapter class sets up (`entry.runtime_data` is the constructed adapter, CONVERSATION
  platform forwarded) and unloads cleanly.
Run `just test` (or `just check`) + `just typecheck`; record the baseline failing set first,
report the delta.

## Risks / open questions
- **Agent-registration checkpoint — RESOLVED:** verified against the installed HA
  source (`.venv/.../components/openai_conversation/conversation.py`, HA 2026.7). Core
  `openai_conversation` — the plan's cited template — STILL subclasses
  `AbstractConversationAgent` and STILL calls
  `conversation.async_set_agent(self.hass, self.entry, self)` /
  `async_unset_agent(self.hass, self.entry)` in `async_added_to_hass` /
  `async_will_remove_from_hass`, keyed on the **parent entry** (not the subentry —
  `async_set_agent` takes a `ConfigEntry` and uses `config_entry.entry_id`). The ticket's
  guess that core "does NOT call `async_set_agent`" was wrong per the installed source, so
  this entity mirrors core verbatim: keeps the base class (with the one
  `reportPrivateImportUsage` pyright-ignore that core's shape forces — `AbstractConversationAgent`
  is namespace-visible but absent from `conversation.__all__`) and the two lifecycle calls
  keyed on `self.entry`. With multiple subentries the legacy agent-manager map is
  last-writer-wins, but that map is only a fallback — routing is per-`entity_id` via the
  entity platform — which is exactly how core behaves; `test_one_entity_per_subentry` and
  `test_setup_and_unload_end_to_end` exercise the full add/remove cycle cleanly.

## Implementation notes (delta from brief — for the reviewer)
- **`const.py` additions** (grouped under a `# --- v1 re-architecture (LLMM-005) ---`
  header, existing v0 constants untouched): `CONF_BACKEND_TYPE`, `CONF_TIMEOUT`,
  `CONF_MEMORY_SCOPE`, `MEMORY_SCOPE_{CONVERSATION,DEVICE,AGENT}`,
  `SUBENTRY_TYPE_CONVERSATION`, `IDLE_TIMEOUT = 30`. `DEFAULT_TIMEOUT = 60` and
  `ERROR_MESSAGE` already existed.
- **`CONF_PROMPT`** is imported from `homeassistant.const` (value `"prompt"`, exactly as
  core openai/ollama do) rather than duplicated in our `const.py` — it is a core HA
  constant, not a name "missing" from HA; the subentry flow (LLMM-007) uses the same core
  key, so there is a single source of truth. (Deviates from the brief's "import from
  const.py" list for this one symbol; noted here per the brief's "note it in the PR" rule.)
- **`build_client_timeout`** placed in `backends/base.py` (the brief's sanctioned "if
  base.py exposes a home, put it there" path) instead of the entity module, because a
  future adapter importing it from `conversation.py` would create an
  adapter→conversation→backends cycle. base.py reaches `const` via an **absolute** import
  (`from custom_components.llm_middleman.const import ...`) — a parent-relative `..const`
  trips ruff `TID252`.
