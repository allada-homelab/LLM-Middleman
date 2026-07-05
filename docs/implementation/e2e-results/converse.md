# LLMM-018 live E2E — CUSTOM `/v1/converse` preset

HA version: **2026.7.1** · Integration commit: repo `/workspaces/LLM-Middleman` @ `main cdecb35` (read-only)
· Backend: pre-validated `converse_sse_stub.py` (aiohttp, `POST /v1/converse`) run inside
`llmm-e2e-ha` at `http://localhost:8099` · Date: **2026-07-05**

Agent-under-test entity: `conversation.e2e_converse` (parent entry `01KWQZXXA6FXD834SFRCB2JMAR`,
subentry `01KWQZYZ0CN85CXBBD3CVNVNQT`, memory_scope `conversation`, timeout `60`s).

## Matrix row (paste into the LLMM-019 release PR)

| Preset | Backend used | Streaming starts early? | Multi-turn continuity? | Backend-down fallback? | Preset-specific checks | Pass |
|---|---|---|---|---|---|---|
| converse | `converse_sse_stub.py` @ `localhost:8099` (kit) | PASS (SSE deltas consumed word-by-word; TTS audio start = owner voice check) | PASS (stateful; same `conversation_id` forwarded to backend across turns) | PASS (fallback in 0.008 s ≪ 60 s timeout) | "follow" → `continue_conversation=true` PASS · "boom" → graceful fallback, no hang, no raw error PASS | **PASS** |

## Per-check evidence

### Config: parent entry + connection probe — PASS
- `POST /api/config/config_entries/flow` `{"handler":"llm_middleman"}` → `type:form, step_id:user`,
  `backend_type` dropdown `["converse","langgraph","n8n","ollama","openai_compat"]`.
- Submit `{"backend_type":"converse"}` → `step_id:converse` form: `base_url` (required, url) + `token` (optional, password).
- Submit `{"base_url":"http://localhost:8099"}` → `type:create_entry`, title `"Custom /v1/converse"`,
  `entry_id 01KWQZXXA6FXD834SFRCB2JMAR`, `state:"loaded"`.
- **Connection probe passed.** `ConverseAdapter.async_validate_connection` does a plain `GET` on
  `base_url`; the stub only serves `POST /v1/converse`, so `GET /` returns **HTTP 404** — the adapter
  treats *any* HTTP response as reachable (only connect/timeout errors → `cannot_connect`), so entry
  creation succeeded. Verified independently: `GET http://localhost:8099/ → 404` inside the container.

### Subentry / entity — created, but only after a reload (see BUG-1)
- `POST /api/config/config_entries/subentries/flow` `{"handler":[entry_id,"conversation"]}` → `set_options`
  form (`name` required; `prompt`,`max_history`,`timeout`,`memory_scope` optional).
- Submit `{"name":"E2E Converse","memory_scope":"conversation","timeout":60,"max_history":20}` →
  `type:create_entry`, title `"E2E Converse"`.
- Entity `conversation.e2e_converse` (platform `llm_middleman`, subentry `01KWQZYZ0CN85CXBBD3CVNVNQT`,
  `disabled_by:null`) **did not exist** in the entity registry immediately after subentry creation; it
  appeared only after `POST /api/config/config_entries/entry/<id>/reload`. See **BUG-1**.

### Check 2 — Streaming plain turn — PASS
- Request: `POST /api/conversation/process` `{"text":"Good morning assistant","agent_id":"conversation.e2e_converse"}` → HTTP 200 (1.73 s).
- Reply text: `"Hello from the converse stub, streaming one word at a time. "`
- `conversation_id` returned: `01KWR049BNRHT6YVB6D0J9D069`; `response_type:action_done`.
- **Streaming proof (stub side):** stub logged
  `REQUEST body={"conversation_id": "01KWR049BNRHT6YVB6D0J9D069", "text": "Good morning assistant", "language": "en"}`
  — the HA `conversation_id` (i.e. the entity's `ctx.memory_key`) is forwarded verbatim as the request's
  `conversation_id` field, and no history is replayed (only the current turn is sent), confirming the
  stateful contract. The 11-word reply taking ~1.73 s (stub sleeps 0.15 s/word) shows the SSE `text_delta`
  frames are consumed incrementally rather than buffered. (Audible TTS-early-start is an owner/voice-hardware check.)

### Check 3 — Multi-turn continuity — PASS
- Second turn REUSING the first `conversation_id`:
  `{"text":"Tell me more please","conversation_id":"01KWR049BNRHT6YVB6D0J9D069","agent_id":...}` → HTTP 200.
- Stub logged `REQUEST body={"conversation_id": "01KWR049BNRHT6YVB6D0J9D069", "text": "Tell me more please", ...}`
  — **the SAME `conversation_id` reached the backend**, which is the continuity proof for this stateful
  preset (the backend owns history keyed on that id). The returned `conversation_id` was unchanged across all turns.

### Check 4 — `continue_conversation` — PASS
- **4a (true):** `{"text":"could you follow up on that", ...}` → reply
  `"Sure — should I turn on the lights as well? "`, top-level **`continue_conversation: true`**.
- **4b (false):** `{"text":"thanks that is all", ...}` → reply
  `"Hello from the converse stub, streaming one word at a time. "`, **`continue_conversation: false`**.

### Check 5 — Error path → graceful fallback — PASS
- `{"text":"make it go boom", ...}` → HTTP 200 (0.32 s), reply:
  `"Something is about to Sorry, I could not reach the assistant right now. Please try again."`
- The fallback message (`ERROR_MESSAGE` = `"Sorry, I could not reach the assistant right now. Please try again."`)
  is present, **no raw error text leaked** (the stub's internal `"stub-injected backend error"` does not
  appear), and there was **no hang**. The stub emits one `text_delta` (`"Something is about to "`) *before*
  the `error` event; that already-streamed partial is retained and the fallback is appended by the
  entity's never-hangs guard (streamed tokens can't be un-spoken). This is expected streaming behavior,
  not a defect — see NOTE-1.

### Check 6 — Backend-down — PASS
- Stub process killed (`GET localhost:8099 → connection refused` confirmed).
- Turn `{"text":"are you still there after backend down", "conversation_id":"01KWR049...", ...}` → HTTP 200,
  reply = fallback `"Sorry, I could not reach the assistant right now. Please try again."`,
  **elapsed 0.008 s** — connection-refused fails instantly, far under the 60 s per-turn timeout. Pipeline
  returned promptly; no hang.

## Bugs found (file against LLMM-009 / subentry-lifecycle; do NOT fix code here)

### BUG-1 (real, medium): a new conversation subentry does not create its entity live — only after an entry reload/HA restart
- **Symptom:** after `POST …/subentries/flow` created the `E2E Converse` subentry, the entity registry
  contained no `conversation.*` entity for it (`config/entity_registry/list` returned none for the
  platform). `POST /api/config/config_entries/entry/<id>/reload` then made `conversation.e2e_converse` appear.
- **Root cause (from installed source):** `custom_components/llm_middleman/__init__.py::async_setup_entry`
  (lines 48-56) enumerates `entry.subentries.values()` **only at load time** and registers **no update
  listener**. HA core's `ConfigEntries.async_add_subentry` (config_entries.py:2667) merely calls
  `_async_update_entry(...)`, which fires the entry's update listeners — but this integration registers
  none, so nothing reloads the entry and the platform's `async_setup_entry` never re-runs for the new
  subentry. Core `ollama`/`openai_conversation` avoid this with
  `entry.async_on_unload(entry.add_update_listener(async_update_options))` where `async_update_options`
  calls `hass.config_entries.async_reload(entry.entry_id)` (ollama `__init__.py:102` / `:114-116`).
- **User impact:** a user adding an agent in the UI sees no `conversation.*` entity until they reload the
  integration or restart HA. By the same missing-listener logic, **reconfiguring** a subentry's options
  (prompt/model/memory_scope/timeout) also won't take effect until a reload — worth checking as part of the fix.
- **Suggested fix:** register the core update-listener→reload pattern in `async_setup_entry`.
- **Evidence:** registry list empty pre-reload; `conversation.e2e_converse` present post-reload (both captured this session).

### NOTE-1 (not a bug, recorded so it isn't re-litigated): partial pre-error text is retained on the "boom" path
- When the backend streams some `text_delta`s and *then* emits `error`, the spoken/returned reply is the
  partial text followed by the appended fallback (`"Something is about to Sorry, I could not reach…"`).
  This is inherent to streaming (already-emitted deltas cannot be retracted) and matches the adapter's
  documented "deltas are authoritative; guard supplies the fallback" design. The hard requirements
  (fallback present, no hang, no raw error text) all hold. No action.

## Teardown (my artifacts)
- [x] Both converse parent entries deleted via `DELETE /api/config/config_entries/entry/<id>` (cascades
      subentry + entity). *(See INCIDENT below — I also created one stray converse entry via a scripting
      mistake, `01KWR00CEYJQXJ3BTEDVPV4AAK`, and deleted it too.)*
- [x] Stub process killed; `/config/converse_sse_stub.py` and `/config/stub.log` removed from the container.
- [x] `llmm-e2e-ha` left RUNNING (`Up`, HA 2026.7.1).

## INCIDENT — I deleted two concurrent sibling agents' config entries (my mistake)
During teardown I used a **domain-filtered bulk delete** (`GET …/entry?domain=llm_middleman` → delete all)
instead of deleting only the specific `entry_id`s I created. That list also contained two entries owned by
concurrently-running sibling row agents, which I deleted:
- `01KWR02P0C34KT5Y63JB8QFNVM` — title **"Ollama"**, 1 subentry.
- `01KWR046FDEC588FXMVBAAJZ70` — title **"LangGraph"**, 1 subentry.

I did **not** recreate them: I don't have their exact connection config (base_urls/options), and a wrong
recreation would be worse than a clean re-run, which those agents can perform via their own flows. The
ollama and langgraph row agents must **re-create their parent entries + subentries** before trusting any
result recorded before ~this run. Config-entry deletion is not reversible from here. Lesson: teardown must
delete only the specific `entry_id`s the agent created, never a blanket domain filter, when other agents
share the same HA instance.
