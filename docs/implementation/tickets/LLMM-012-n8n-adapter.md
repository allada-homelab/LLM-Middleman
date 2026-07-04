---
id: LLMM-012
title: n8n adapter (Chat Trigger/plain webhook, dual streaming/blocking)
status: done
phase: 2
depends_on: [LLMM-003, LLMM-004]
---

# LLMM-012 — n8n adapter (Chat Trigger/plain webhook, dual streaming/blocking)

## Context
First-class n8n preset (plan.md §Context "User decisions", §Architecture `backends/n8n.py`,
§Per-connector configuration matrix **n8n** row, §Serialization). n8n is the one connector
that needs a **blocking fallback**: its streaming silently degrades to a single blocking
JSON body when the workflow isn't stream-enabled on **both** the Chat Trigger and the AI
Agent node — so the adapter must branch on the **actual response content-type / first
bytes, not the config toggle**. Streaming is **NDJSON `StructuredChunk`** (n8n 1.103.0),
NOT SSE — this ticket does not use `_sse.py`. Stateful: HA `conversation_id` (memory_scope
key) → n8n `sessionId`, which its memory nodes partition on. Closest prior art:
EuleMitKeule/webhook-conversation (research-2) — copy its configurable output-field + 1–300 s
timeout UX and NDJSON begin/item/end envelope, and its logged mistakes (session id must
reach the backend; multiple begin/end blocks per run).

## Scope
**In:**
- `custom_components/llm_middleman/backends/n8n.py` implementing the `BackendAdapter` ABC;
  `backend_type = "n8n"`; `supports_ha_tools = False`; stateful (`sessionId` = session key).
- `async_validate_connection`: **no reliable network probe** (the webhook is opaque and a
  POST would trigger the live workflow) — validate the URL shape only; document why.
- `stream_turn(self, chat_log, user_input, ctx: TurnContext)`: `POST {webhook_url}` with the
  request body below, then **branch on the actual response** (content-type / first bytes), not
  the `streaming` toggle:
  - **NDJSON stream** → parse `StructuredChunk` lines, accumulate `item.content`, **EOF is
    the true done signal** (never treat `end` as terminal — multiple begin/end cycles per
    run).
  - **Blocking JSON** → parse one JSON object, read the reply via the output-field fallback
    chain.
- `target_type` gating the `action` field; auth `none` / `basic` / custom `header`;
  configurable `input_field` / `output_field` / `session_field`; optional system prompt as
  an extra body field; misconfig visibility (never silently speak raw JSON).

**Out:**
- HA tools (n8n workflow owns its tools/agent server-side).
- Parent connection form + subentry option schema — LLMM-006/LLMM-007.
- Memory-scope key derivation — entity (LLMM-005); this ticket consumes `ctx.memory_key`
  and sends it as `sessionId`.

## Implementation notes
- **Request body:** `POST {webhook_url}` (opaque full production chat URL,
  `/webhook/<id>/chat` — user pastes it verbatim; no path appended) with
  `{"action": "sendMessage", "<session_field>": <session_key>, "<input_field>": text}`.
  Omit `"action"` when `target_type` is **plain Webhook + Respond-to-Webhook** (only Chat
  Trigger uses `action`). Add the optional system prompt as an extra body field when set.
  Serialize any HA object with `json.dumps(..., default=str)` (plan §Serialization).
- **Field defaults:** `input_field` default `chatInput` (the node's `chatInputKey` is
  configurable); `output_field` default `output` with fallback `text`; `session_field`
  default `sessionId`.
- **Branch on the real response (critical):** read `Content-Type` and/or sniff the first
  bytes. If it is NDJSON / `application/json-lines` / begins with a `StructuredChunk`
  object → stream path. Otherwise treat as a single blocking JSON body. **Do not trust the
  `streaming` config toggle** — n8n sends a blocking body when the workflow isn't
  stream-enabled on both nodes, and treating that as a stream produces silence. An HTML
  body (proxy/gateway timeout) → `BackendStreamError` (guard fallback).
- **NDJSON StructuredChunk parser (inside `n8n.py`, not `_sse.py`):** buffer raw bytes,
  split on `\n`, `json.loads` each complete line. Chunk shape:
  `{"type":"begin"|"item"|"end"|"error","content":…}`. Accumulate `content` from `item`
  chunks; emit role-first `{"role":"assistant"}` then `{"content": chunk_content}` as they
  arrive (never buffer to end — voice latency). **EOF (stream closes) is the true done
  signal**; `end` only closes one logical segment (a run may emit multiple begin/end
  cycles — webhook-conversation #38/#39). `error` chunks → `BackendStreamError`. Do not
  strip whitespace.
- **Blocking mode:** parse the single JSON object; resolve the reply via the fallback
  chain `result.get(output_field)` → `result.get("text")`. Optional `continueConversation`
  boolean output field → when present and true, set `ctx.continue_conversation = True` (same
  `TurnContext` channel as converse; LLMM-003/LLMM-005). **Misconfig visibility:** if neither
  output field is present,
  surface an error (mirrors n8n's "wrong key returns the whole object") — **never
  `json.dumps` the whole object and speak it**.
- **Auth:** `none`; `basic` (`aiohttp.BasicAuth`); custom `header` (arbitrary header
  name+value — covers plain-webhook Header Auth and reverse proxies). Redact credentials
  from logs.
- **Session:** send `ctx.memory_key` as `<session_field>`; n8n memory nodes partition on it
  (research-2: failing to forward it breaks backend memory).
- **Timeout default 30 s** (n8n row) — lower than the 60 s global default.
- **Const keys:** add `CONF_WEBHOOK_URL`, `CONF_TARGET_TYPE` (+ `TARGET_CHAT_TRIGGER` /
  `TARGET_PLAIN_WEBHOOK`), `CONF_N8N_AUTH_TYPE` (+ `none`/`basic`/`header` values),
  `CONF_STREAMING`, `CONF_INPUT_FIELD` (`chatInput`), `CONF_OUTPUT_FIELD` (`output`),
  `CONF_SESSION_FIELD` (`sessionId`), `BACKEND_N8N = "n8n"`.

## Acceptance criteria
- [x] `N8nAdapter(BackendAdapter)` with `backend_type = "n8n"`,
      `supports_ha_tools = False`, registered in `BACKEND_TO_CLS`.
- [x] Request POSTs `{webhook_url}` with `{<session_field>, <input_field>}` (+ `action`
      only for Chat Trigger) and optional system-prompt field.
- [x] The adapter branches on the **actual response content-type/first bytes**, not the
      `streaming` toggle; a blocking body from a "streaming" config is handled correctly.
- [x] NDJSON path accumulates `item.content`, streams deltas immediately, and treats **EOF
      (not `end`) as done**, tolerating multiple begin/end cycles.
- [x] `error` chunks and HTML bodies → `BackendStreamError` (guard fallback).
- [x] Blocking path reads `output_field` → `text` fallback chain; missing output field →
      surfaced error, never raw JSON spoken.
- [x] Auth `none`/`basic`/`header` all work; credentials redacted from logs.
- [x] `sessionId` = `ctx.memory_key`; a truthy `continueConversation` output field sets
      `ctx.continue_conversation`; timeout defaults to 30 s.
- [x] Gates green: `just check` + `just typecheck`.

## Verification
Write `tests/backends/test_n8n.py` driving **raw bytes** through the real parsers (split
chunks — plan §Verification, plan §Implementation phases "n8n additionally tested for the
blocking-response path and the wrong-mode mismatch"):
- **Streaming happy path:** bytes
  `{"type":"begin"}\n{"type":"item","content":"Hel"}\n{"type":"item","content":"lo"}\n{"type":"end"}\n`
  with boundaries **split mid-object**, connection then closes (EOF). Assert deltas
  `[{"role":"assistant"},{"content":"Hel"},{"content":"lo"}]` and done at EOF.
- **Multiple begin/end cycles:** two begin…end blocks before EOF → all `item.content`
  emitted; the first `end` is not treated as terminal.
- **Wrong-mode mismatch:** `streaming` config ON but the response is a single blocking JSON
  body (`{"output":"done"}`, `Content-Type: application/json`) → adapter detects it and
  emits `{"content":"done"}`.
- **Blocking output fallback:** `{"text":"hi"}` with default `output_field="output"` →
  falls back to `text`; `{"foo":"bar"}` → surfaced error, **not** spoken JSON.
- **error chunk:** `{"type":"error","content":"boom"}` → `BackendStreamError`.
- **HTML body:** a `<html>…504…</html>` response → `BackendStreamError`.
- **Action gating:** Chat Trigger includes `"action":"sendMessage"`; plain Webhook omits it.
- **Auth:** `basic` sets `BasicAuth`; `header` sets the configured header; both redacted in
  logged output.
Run `just check` + `just typecheck`; record delta vs baseline.

### Verification evidence (executed)
`tests/backends/test_n8n.py` (23 tests) drives raw bytes through the real parser via the
conftest harness and covers every case above (streaming happy path split mid-object,
multiple begin/end cycles, whitespace-preserved content, wrong-mode mismatch, blocking
output→text fallback + custom field + pretty-printed body, missing output field surfaced,
`continueConversation`, error chunk, HTML body, malformed line skipped, silent stream end,
action gating, basic/header auth + redaction, 30 s default timeout, factory registration,
URL-shape validation).

- `just check` → `78 passed, 2 warnings` (baseline 55 → +23 new; no existing test
  regressed). The 2 warnings are aiohttp's `BasicAuth` deprecation (see Risks).
- `just typecheck` (basedpyright strict) → `0 errors, 0 warnings, 0 notes`.
- lint (`ruff check`) → `All checks passed!`; `ruff format --check` → clean;
  `uv lock --check` → up to date.

Note: `tests/test_backends_base.py::test_factory_empty_registry_raises` (LLMM-003) asserted
the registry was empty "until the first adapter"; since LLMM-012 is that first adapter, it
was renamed to `test_factory_unknown_type_raises` and now asserts an *unregistered* type
still raises while n8n resolves.

## Risks / open questions
- **No connection probe** is a deliberate trade — the webhook is opaque and POSTing during
  config would fire the live workflow. E2E (LLMM-018) is the real validation. Document this
  in the parent flow's help text (LLMM-006).
- **`StructuredChunk` shape is pinned to n8n 1.103.0** — verify field names against the
  target n8n version during E2E; the branch-on-real-response logic must fail soft if the
  shape drifts.
- **`continueConversation` output field** sets `ctx.continue_conversation` via the
  `TurnContext` channel (LLMM-003) — the same seam converse uses; no cross-ticket
  coordination is outstanding.
- **`aiohttp.BasicAuth` is deprecated** in the installed aiohttp (removal in 4.0; warning
  emitted at test time). This ticket keeps `BasicAuth` as the plan/base.py convention
  prescribes; migrating basic auth to `aiohttp.encode_basic_auth()` + an `Authorization`
  header is a small, mechanical follow-up to do repo-wide (not just here) so the
  convention stays consistent across adapters.
