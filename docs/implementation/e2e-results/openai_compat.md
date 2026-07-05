# LLMM-018 live E2E results — OPENAI_COMPAT (`/v1/chat/completions`) preset

HA version: **2026.7.1** · Integration commit: **cdecb35** · Date: **2026-07-05**
Backend: the same local **ollama** container's **OpenAI-compatible `/v1` shim**
(`llmm-e2e-ollama`, model **qwen3:0.6b**), reachable from HA at the ollama bridge IP
`172.17.0.7` (see the ollama results for the IP-instability note).

## Result: PASS (tool-call = model-limited, adapter path verified correct) — plus one UX gotcha filed to LLMM-010

| Check | Result | Evidence |
|---|---|---|
| Parent entry create + probe (`GET /v1/models`) | **PASS** | `backend_type=openai_compat`, `base_url=http://172.17.0.7:11434/` (**trailing slash**), `api_key="dummy-key-not-real"` → `create_entry`. |
| **Trailing-slash URL normalization** | **PASS** | Submitted base_url **ends in `/`**; the flow `rstrip("/")` normalized it and the probe `GET …/v1/models` resolved (200) → `create_entry`. A non-normalized slash would 404 `//v1/models`. |
| **Dummy/empty API-key handling** | **PASS** | A junk key `"dummy-key-not-real"` was accepted (ollama's `/v1` ignores auth); adapter sends it as a Bearer header only when set. Turns succeeded with it. |
| Subentry model dropdown from `/v1/models` | **PASS** | `model` field = `select`, options `['qwen2.5:1.5b-instruct','qwen3:0.6b']` — sourced from `GET /v1/models` `data[].id`. |
| `llm_hass_api` option offered (supports_ha_tools) | **PASS** | offered `['assist']`. `memory_scope` correctly **absent** (stateless-replay). |
| Streaming (transport-level) | **PASS** | Direct `POST /v1/chat/completions {stream:true}` → **269 incremental SSE `data:` chunks**, terminated by literal `data: [DONE]`. This is the delta stream the adapter reads via `_sse.async_iter_sse` and yields per-fragment → streaming TTS. (TTS-early audio is owner/voice-observable, not assertable over the buffering REST endpoint.) |
| Streaming turn via `/api/conversation/process` | **PASS** | `conversation.e2e_openai` + "Say hello in one short sentence." → 200 in 6.0s, reply present. |
| Multi-turn continuity (same conversation_id) | **PASS** | Turn 1 "Remember: my secret code word is MANGO7…" (cid `01KWR153TECBTC5V12V9T0BC4J`) → ack. Turn 2 (same cid) → **`"MANGO7"`**. Correct recall proves ChatLog replay across turns. |
| **Tool call** — utterance flips `input_boolean.llmm_e2e_test` | **MODEL-LIMITED** (adapter PASS) | See below. |
| Backend-down fallback | **PASS** | `docker pause llmm-e2e-ollama` (IP preserved) + short-timeout agent `conversation.e2e_openai_down` (timeout=15s) → "Say hello." returned 200 in **15.6s** (bounded by the deadline, no hang), reply = fallback **"Sorry, I could not reach the assistant right now. Please try again."** `docker unpause` restored it with the IP intact. |

## Tool call — MODEL-LIMITED, not an adapter bug

Utterance "turn on llmm e2e test" (3 phrasings) on `conversation.e2e_openai` (qwen3:0.6b) —
**helper stayed `off`**. Fresh HA log warnings during these calls:
`intent: Received invalid slot info for HassTurnOn: … @ data['device_class']['value'][0]`, and
the model's own replies literally cite **"InvalidSlotInfo … slot parameters are invalid for
HassTurnOn."**

**Why this is the model, not the adapter:** the HA slot WARNING firing proves the openai_compat
adapter formatted the HA tools, forwarded them, **accumulated the streamed `delta.tool_calls`
fragments by index, emitted `llm.ToolInput`, and invoked the HA intent** (LLMM-014 tool loop) —
the loop even re-entered with the "InvalidSlotInfo" error handed back to the model. Same
root cause as the ollama row: the small model supplies an invalid `device_class` slot. Direct
`intent/handle` by name and the built-in agent both flip the helper (proving entity/exposure/
adapter path correct). A capable tool model is needed to observe the end-to-end flip.

## Bug filed → LLMM-010 (openai_compat adapter): base_url must be the server ROOT, `/v1` is hardcoded

**Severity: medium (config-time UX gotcha).** The adapter hardcodes the `/v1` path prefix:
`_fetch_models` requests `f"{base_url}/v1/models"` and `stream_turn` posts to
`f"{base_url}/v1/chat/completions"`. So `base_url` must be the server **root**
(`http://host:11434`). Entering the **conventional OpenAI-style base_url that includes `/v1`**
(`http://host:11434/v1`, which is what OpenAI, LM Studio, vLLM, LocalAI users normally paste,
and what this task brief itself specified) double-paths to `/v1/v1/models` → **404 →
`cannot_connect` at config time**. Verified live: `GET /v1/models`=200, `GET /v1/v1/models`=404,
and the `/v1` base_url produced `errors:{base:"cannot_connect"}` on the parent flow.

- **Impact:** OpenAI's *own* API base URL is `https://api.openai.com/v1` — a user copying the
  standard base_url gets a confusing "cannot connect" with no hint that the `/v1` is doubled.
- **Fix options for LLMM-010:** (a) tolerate a trailing `/v1` (strip it before appending), or
  (b) document in `strings.json`/field description that base_url is the server root without
  `/v1`. The trailing-*slash* normalization is fine and separate; this is a path-*segment* issue.

## Live inventory at handoff
- Parent entry `01KWR14X0YSK4AB27RXQYAZ1BA` "OpenAI-compatible" (base_url normalized to
  `http://172.17.0.7:11434`) → subentries "E2E OpenAI" (`conversation.e2e_openai`) and
  "E2E OpenAI down" (`conversation.e2e_openai_down`, timeout=15 — used for backend-down).
- Recreate recipe: parent `backend_type=openai_compat`, `base_url=http://<ollama-ip>:11434`
  (ROOT, no `/v1`), any dummy key; subentry model `qwen3:0.6b`, `llm_hass_api=[assist]`.
