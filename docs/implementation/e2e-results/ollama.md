# LLMM-018 live E2E results — OLLAMA (native `/api/chat`) preset

HA version: **2026.7.1** · Integration commit: **cdecb35** · Date: **2026-07-05**
Backend: local **ollama** container `llmm-e2e-ollama` (`ollama/ollama:latest`), model
**qwen3:0.6b** (`capabilities: ["completion","tools","thinking"]`), reachable from HA
(`llmm-e2e-ha`) at its docker bridge IP. **NOTE:** the ollama IP changed `172.17.0.8 → 172.17.0.7`
after a `docker restart` (docker reassigns bridge IPs); config entries pin an IP, so a
restart invalidates them — see Caveats. All values below are against the live IP at test time.

## Result: PASS (tool-call = model-limited, adapter path verified correct)

| Check | Result | Evidence |
|---|---|---|
| Parent entry create + probe (`GET /api/tags`) | **PASS** | `backend_type=ollama`, `base_url=http://172.17.0.7:11434` → flow returned `create_entry`, entry `state=loaded`. A bad URL returns `cannot_connect`; this passed the real probe. |
| Subentry model dropdown from `/api/tags` | **PASS** | subentry `set_options` form `model` field = `select` with options `['qwen3:0.6b']` (later `['qwen2.5:1.5b-instruct','qwen3:0.6b']` after 2nd pull) — sourced from `/api/tags`. |
| `llm_hass_api` option offered (supports_ha_tools) | **PASS** | form `llm_hass_api` field offered `[{'value':'assist','label':'Assist'}]`. `memory_scope` field correctly **absent** (ollama is stateless-replay). |
| Streaming (transport-level) | **PASS** | Direct `POST /api/chat {stream:true}` → **54 incremental NDJSON objects**, first content delta ~0.0s, terminated by `done:true`. This is the per-token stream the adapter frames (`_iter_ndjson`) and yields as deltas → streaming TTS. (TTS-early audio is owner/voice-observable — not assertable over REST, which buffers the reply.) |
| Streaming turn via `/api/conversation/process` | **PASS** | `conversation.e2e_ollama` + "Say hello in one short sentence." → 200 in 6.9s, reply `"Hello! 😊"`. |
| Multi-turn continuity (same conversation_id) | **PASS** | Turn 1 "Remember: my secret code word is BANANA42…" (cid `01KWR09DHWBTAGPGG5QXJVMF8V`) → ack. Turn 2 (same cid) "What was the secret code word?" → **`"BANANA42."`**. Correct recall proves prior-turn messages were replayed to ollama (stateless-replay rebuilds `messages[]` from ChatLog). |
| **Tool call** — utterance flips `input_boolean.llmm_e2e_test` | **MODEL-LIMITED** (adapter PASS) | See below. |
| Backend-down fallback | **PASS** | `docker stop llmm-e2e-ollama` mid-session → "Say hello." returned 200 in **3.1s** (no hang), reply = fallback **"Sorry, I could not reach the assistant right now. Please try again."** HA log: `conversation.py … Backend stream failed; returning fallback message` from `ollama.py stream_turn`. `docker start` restored it. |

## Tool call — MODEL-LIMITED, not an adapter bug

Utterance "turn on the llmm e2e test" (+ 2 more phrasings) on **qwen3:0.6b**, then 2 phrasings
on fallback **qwen2.5:1.5b-instruct**, then 3 phrasings on qwen2.5 with a slot-constraining
system prompt — **8 attempts, helper stayed `off`** every time.

**Root cause (ground-truth captured):** HA log during every attempt:
`homeassistant.helpers.intent: Received invalid slot info for HassTurnOn: value must be one of
['awning','blind',…,'switch','tv',…] @ data['device_class']['value'][0]`. Both small models
call `HassTurnOn` but populate an **invalid `device_class` slot**, which HA's intent rejects.

**Why this is the model, not the adapter — proven three ways:**
1. **Direct intent** `POST /api/intent/handle {name:HassTurnOn, data:{name:"llmm e2e test"}}` →
   `success:[{id:"input_boolean.llmm_e2e_test"}]`, helper flipped **on**. Entity is matchable by name.
2. **Built-in agent** `conversation.home_assistant` + "turn on llmm e2e test" → "Turned on the
   switch", helper **on**. Exposure/naming/intent all correct.
3. The HA slot WARNING firing **is proof the adapter formatted the tools, forwarded them,
   parsed the streamed `tool_calls`, and invoked the HA intent** — the full LLMM-015 tool loop
   executed (the model even received the "InvalidSlotInfo" error back and retried). Only the
   model's slot *value* was wrong. `_parse_tool_args` correctly drops `None`/`""` and repairs
   stringified JSON; it does not (and should not) strip a non-empty invalid enum — this matches
   core HA ollama behavior.

**Prerequisite discovered:** the test helper was **not exposed to Assist** by default
(exposure was `None`). Exposed it via WS `homeassistant/expose_entity`
(`{assistants:["conversation"], should_expose:true}`) before the tool tests — expected HA
behavior, recorded for reproducibility, not a defect.

Verdict: tool-call rows for both tool-capable presets are **model-limited on a 0.6–1.5B local
model**; the adapter tool pipeline is verified correct. A capable tool model (e.g. a larger
qwen2.5-instruct or a hosted model) is needed to see the helper flip end-to-end.

## Caveats / notes
- **ollama bridge IP is not stable across `docker restart`** (`.0.8 → .0.7` observed). Entries
  pin an IP, so a restart breaks them. Kept the container running (paused/unpaused for the
  openai down-test to preserve the IP) after the ollama backend-down `stop/start`, which is
  why the live entry was recreated at `.0.7`.
- A subentry added over REST needs an entry **reload** before its `conversation.*` entity
  registers (called `POST …/entry/{id}/reload`). Likely a REST-driving artifact (the UI
  auto-reloads); not asserted as a bug.

## Live inventory at handoff
- Parent entry `01KWR114GDKDXHFW5HC46BASCD` "Ollama" → subentry "E2E Ollama"
  (`conversation.e2e_ollama`, model qwen3:0.6b, llm_hass_api=[assist]).
- Recreate recipe (if torn down): `scratchpad/run_ollama.py http://<ollama-ip>:11434 ollama "E2E Ollama"`.

## Bugs filed
- None against the ollama adapter (LLMM-015). Tool-call failure is a documented small-model
  limitation; adapter path verified correct.
