# LLMM-018 live E2E results — dress-rehearsal matrix (agent-run, 2026-07-05)

HA: 2026.7.1 (disposable container `llmm-e2e-ha`, image sha256:f73512ba…) · Integration: main @ `cdecb35` ·
Method: all turns driven through the real HA conversation API (`/api/conversation/process`), config/subentry
flows through the real flow APIs; per-row detail + raw evidence in the sibling files of this directory.
Every row 2/2-adversarially verified live (converse's refutation was a teardown-process foul; its results
were independently re-driven and confirmed exact — see Incidents).

| Preset | Backend used | Streaming | Continuity | Backend-down fallback | Preset-specific | Pass |
|---|---|---|---|---|---|---|
| converse | pre-validated SSE stub (in-container) | PASS (word-by-word deltas; memory_key forwarded verbatim as conversation_id) | PASS (same conversation_id at the stub across turns) | PASS (fallback in 0.02 s) | done.continue_conversation → ConversationResult true/false ✓ · error event → fallback, no raw error leaked ✓ | **PASS** |
| langgraph | real `langgraph dev` (langgraph-api 0.10.0), MessagesState graph | PASS (messages-tuple token frames reassembled through HA) | PASS (both runs on the SAME thread_id) | PASS | **Frame-shape verdict: MATCH** (field-by-field vs parser; raw bytes in `langgraph-raw-capture.txt`) · finding: no `event: end` on success — EOF terminates (dead code cleaned up post-E2E) | **PASS** |
| ollama | local ollama, qwen3:0.6b (+qwen2.5:1.5b) | PASS (54 incremental NDJSON objects, done:true) | PASS (BANANA42 recall = ChatLog replay proven) | PASS (3.1 s, guard log confirmed) | model dropdown from /api/tags ✓ · llm_hass_api offered ✓ · memory_scope correctly absent ✓ · tool call: **model-limited** (tools sent correctly; tiny model never emits tool_calls — retest w/ capable model) | **PASS** |
| openai_compat | same ollama via /v1 | PASS (269 SSE chunks, [DONE]) | PASS (MANGO7 recall) | PASS (bounded by 15 s agent timeout) | trailing-slash normalization ✓ · dummy API key ✓ · model dropdown from /v1/models ✓ · tool call: model-limited (as above) · found /v1-in-base_url UX trap (fixed post-E2E) | **PASS** |
| n8n | real n8n, Chat Trigger→AI Agent (model = ollama /v1) | PASS (NDJSON StructuredChunk) | PASS (sessionId = conversation_id in n8n executions) | PASS (22.7 s) | **wrong-mode mismatch (load-bearing): blocking body content-sniffed and answered ✓** · missing output field → fallback, never raw JSON ✓ · wire protocol conforms to n8n.py (raw capture) | **PASS** |
| HACS packaging | published GitHub main.zip → fresh consumer HA | — | — | — | zip layout/hacs.json/manifest all valid ✓ · zip-installed integration loads, 5-option flow ✓ · manifest 0.1.0 recorded for release bump | **PASS** |

## Bugs found by the rehearsal (the point of the exercise) — all fixed in Wave E2E-C
1. **Subentry lifecycle (medium)**: new conversation subentry's entity only appeared after entry reload → fix PR (LLMM-007 amendment), live re-verified.
2. **openai_compat `/v1` base_url trap**: `http://host/v1` input → `/v1/v1/...` → fix PR (flow normalization + hint, LLMM-008 amendment).
3. **langgraph `event: end` dead code**: real server terminates on EOF → cleanup PR (LLMM-011 amendment).

## Deferred to owner (Tier 1/2 — cannot be observed from the devcontainer)
- Tool-call rows with a tool-capable model (owner's llama.cpp proxy) — adapter side verified correct (tools present in requests; failure isolated to tiny-model capability).
- Live-HA install + rows on the owner's instance; HACS-frontend device-flow install (60 s interactive).
- Voice hardware: streaming-TTS time-to-first-audio, wake-word follow-up with mic-open (`continue_conversation`).

## Incidents
- Converse row's teardown bulk-deleted sibling rows' config entries mid-run (violating "your artifacts only"); affected rows re-ran and re-verified green. No effect on final results; flagged for orchestration hygiene.

## Teardown
Performed after Wave E2E-C's live re-verification: `docker rm -f llmm-e2e-ha llmm-e2e-ollama llmm-e2e-n8n` + volumes (`llmm-e2e-ha-config`, `llmm-e2e-ollama-models`, n8n's), stub files removed. (`llmm-e2e-ha-hacs` + its volume were removed by its own row agent.)
