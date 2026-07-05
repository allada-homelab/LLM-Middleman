# LLMM-018 live E2E results — n8n preset

HA version: **2026.7.1** · Integration commit: **cdecb35** · Date: **2026-07-05**
Backend: **n8n `n8nio/n8n` v2.28.6** (bridge `172.17.0.9:5678`) → AI Agent → OpenAI-compat
model **qwen3:0.6b** via the parallel `llmm-e2e-ollama` (`172.17.0.8:11434/v1`).
Adapter under test: `custom_components/llm_middleman/backends/n8n.py`.
Raw wire evidence: `results/n8n-raw-capture.txt`.

## Matrix row

| Preset | Backend used | Streaming starts early? | Multi-turn continuity? | Backend-down fallback? | Preset-specific checks | Pass |
|---|---|---|---|---|---|---|
| n8n | stock Chat Trigger→AI Agent (qwen3:0.6b via ollama) | YES (NDJSON, see note) | YES (sessionId=conversation_id) | YES (fallback, 22.7s) | stream→NDJSON chunks ✓ · NOT-stream + toggle ON → blocking body answered ✓ · sessionId continuity ✓ · missing output → fallback, never raw JSON ✓ | **PASS** |

## Per-check detail

### 1. Raw protocol capture (direct POST to production Chat Trigger webhook) — PASS
Body `{action:sendMessage, sessionId, chatInput}` posted directly to each webhook. Full
bytes in `results/n8n-raw-capture.txt`. Field-by-field vs `backends/n8n.py`:

- **STREAM workflow** (Chat Trigger `responseMode=streaming` + AI Agent `enableStreaming=true`):
  HTTP 200, `Content-Type: application/json`, **172 NDJSON lines** — histogram `begin=1,
  item=170, end=1, error=0, malformed=0`. First line `{"type":"begin",...}`, last line
  `{"type":"end",...}`, `item.content` accumulates to `"Hi there!"` (most items are empty
  content = qwen3 thinking deltas). Matches `_sniff_mode` (first-line `type` in
  `_STRUCTURED_CHUNK_TYPES` → `ndjson`, L91), `_line_contents` (`item`→content, `begin`/`end`
  non-terminal, EOF = done). **No mismatch.**
- **BLOCKING workflow** (Chat Trigger `responseMode=lastNode`): HTTP 200,
  `Content-Type: application/json`, single object **`{"output":"Hi there!"}`** (22 bytes).
  No `type` key + non-ndjson CT → `_sniff_mode`→`blocking` (L90-95); `_blocking_deltas`
  reads `obj["output"]` (L257-267). **No mismatch.**
- **NO-OUTPUT workflow** (lastNode → Edit Fields): **`{"wrongField":"surprise, no output field here"}`**.
  `obj["output"]`/`obj["text"]` both `None` → `_blocking_deltas` **raises**
  `BackendStreamError "n8n reply missing 'output'/'text' field"` (L259-263); never dumps the
  raw object.

> **No LLMM-012 bug from the wire protocol.** All three shapes conform to `backends/n8n.py`.
> Only caveat is *timing*: the blocking reply took ~48.7 s (qwen3 emits all thinking before
> returning) which exceeds the adapter's `N8N_DEFAULT_TIMEOUT=30`; handled by raising the
> subentry `CONF_TIMEOUT` to 180 for the wrong-mode HA test — a rig timing note, not a defect.

### 2. HA streaming turn (STREAM-ENABLED workflow) — PASS
Parent entry `backend_type=n8n`, `webhook_url=<stream chat URL>`, `target_type=chat_trigger`,
`auth_type=none`. Subentry **"E2E n8n"** (`timeout=180`, `max_history=10`,
`memory_scope=conversation`) → entity `conversation.e2e_n8n`.
`POST /api/conversation/process {text:"Reply with a short greeting.", agent_id:"conversation.e2e_n8n"}`
→ HTTP 200, 5.1 s, speech **"Hello! 😊 How can I assist you today?"** (clean text, not raw
JSON). Reply assembled from the NDJSON stream.

- *"Streaming starts early"* is verified **at the wire level** (raw capture: 172 chunks
  streamed incrementally over ~13 s; adapter yields `{role:assistant}` then per-`item`
  `{content}` deltas as they arrive). The REST `/conversation/process` endpoint **buffers**
  the full result, so exact first-token TTS timing was **not** measured through HA (would
  need the assist_pipeline SSE / a real satellite — owner-run row). Marked YES on the wire
  evidence, with that honest limitation.

### 3. Wrong-mode mismatch (load-bearing) — PASS
Reconfigured the SAME parent entry's `webhook_url` to the **NOT-stream-enabled (blocking)**
workflow (there is **no streaming toggle** to keep "ON" — see finding below; the adapter
always content-sniffs, which is exactly what this proves). Turn →
HTTP 200, speech **"您好！"** (qwen replied in Chinese), `response_type=action_done`. The
single JSON blocking body `{"output":"您好！"}` was detected by content sniffing and spoken as
clean text — **no crash, no raw JSON**. Confirms `_sniff_mode` branches on the real response,
not on any config toggle.

### 4. sessionId continuity — PASS
Two turns with the same `conversation_id` `01KWR0KB4JWMNN3CC0MDBEP48R` (memory_scope=conversation).
n8n REST execution data (`/rest/executions/{6,7}?includeData=true`, deflattened) shows **both
executions carried `sessionId = 01KWR0KB4JWMNN3CC0MDBEP48R`** = the HA conversation_id. Proves
the adapter sends `ctx.memory_key` as the session field consistently across turns.
(The model didn't *recall* the name — the test workflow has no memory node — but that is not
this check; the check is a stable sessionId, which holds.)

### 5. Missing output field — PASS
Entry pointed at the NO-OUTPUT workflow (`{"wrongField":...}`). Turn → HTTP 200, speech
**"Sorry, I could not reach the assistant right now. Please try again."** (the `ERROR_MESSAGE`
fallback), `response_type=action_done`. **Never** raw JSON; the `wrongField` body is not
leaked. Adapter raised `BackendStreamError`, `conversation.py::_guarded` surfaced the fallback.

### 6. Backend-down — PASS
`docker stop llmm-e2e-n8n` (entry pointed at the stream URL), then a turn →
HTTP 200, speech = the same fallback message, returned in **22.7 s** (TCP connect timeout to
the now-dead bridge IP — no listener; returned with fallback, pipeline **did not hang**). Not
instant (connect timeout, not RST), but well within the deadline.

## Findings (candidate for LLMM-012 / adjacent tickets)

- **No wire-protocol bug.** Streaming NDJSON `StructuredChunk`, blocking JSON, and
  missing-field handling all match `backends/n8n.py` exactly (n8n v2.28.6 ≥ 1.103.0).
- **`CONF_STREAMING` is defined-but-unused (dead config).** `const.py:109` defines
  `CONF_STREAMING` with a comment calling it "a config-flow help-text toggle only," but the
  subentry `set_options` form exposes only `{name, prompt, max_history, timeout, memory_scope}`
  — **no streaming field is rendered**, and no adapter reads it. The brief's "streaming toggle
  ON" has no UI/config surface; the adapter always content-sniffs (which is correct). Minor:
  either wire the help-text toggle into the n8n form or drop the unused const. Not a functional
  defect — the sniffing design makes a toggle unnecessary — but it is a Potemkin config knob.
- **Subentry add does not create the entity until the parent entry is reloaded.**
  `conversation.py::async_setup_entry` enumerates `config_entry.subentries` only at setup time
  and registers no new-subentry listener, so `conversation.e2e_n8n` appeared **only after an
  explicit `POST .../entry/{id}/reload`**. This is **not n8n-specific** (all presets share this
  entity path) — flag for whoever owns LLMM-007/005 to confirm whether the real UI flow
  auto-reloads (core ollama/openai register an update listener that this integration appears to
  lack). Load-bearing to verify before release; low blast (a reload fixes it).

## Timing note (voice UX)
qwen3:0.6b **blocking** replies took 30–49 s (all thinking emitted before the single body),
vs ~5–13 s **streaming**. `N8N_DEFAULT_TIMEOUT=30` would time out slow blocking replies on a
default subentry — real deployments pairing n8n-blocking with a slow local model should raise
`CONF_TIMEOUT`. Streaming stays well under the default.

## Environment / bootstrap notes (for reproduction)
- n8n **failed to start** with the plain `docker run … n8nio/n8n` from the brief: it binds to
  `::` (IPv6) and exits — fixed with `-e N8N_LISTEN_ADDRESS=0.0.0.0 -e N8N_HOST=0.0.0.0`.
- **Headless bootstrap succeeded via the internal `/rest/*` API on the FIRST approach**
  (no 3-approach fallback needed): `POST /rest/owner/setup {email,firstName,lastName,password}`
  → 200 + `n8n-auth` cookie; `POST /rest/login {emailOrLdapLoginId,password}` for the session.
- **n8n 2.x publish model:** workflows now have `activeVersion`/`activeVersionId`; a plain
  `PATCH /rest/workflows/{id} {active:true}` does **not** activate (stays `active:false`).
  Activation is `POST /rest/workflows/{id}/activate {versionId:<current versionId>}` → 200,
  `active:true`, `triggerCount:1`. Ollama credential: `POST /rest/credentials
  {type:openAiApi, data:{apiKey:"ollama", url:"http://172.17.0.8:11434/v1"}}`.
- Production chat webhook URL = `http://172.17.0.9:5678/webhook/<chatTrigger.webhookId>/chat`.

## Teardown
- [x] Deleted my HA parent entry (`01KWR0C07NTBN2H0EA5X561GYW`) + its "E2E n8n" subentry;
      `conversation.e2e_n8n` removed. No other agents' entries touched.
- [x] `llmm-e2e-n8n` left **stopped** (`docker stop`, exit 0) — still present (not removed) so
      its 3 workflows/executions remain inspectable by verifiers; `docker start llmm-e2e-n8n`
      restores it.
- [x] Did **not** touch `llmm-e2e-ollama` (it exited on its own ~when the parallel ollama agent
      finished; all my captures were already complete).
