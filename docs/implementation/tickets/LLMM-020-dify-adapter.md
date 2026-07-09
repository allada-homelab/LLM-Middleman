---
id: LLMM-020
title: Dify adapter (chat/agent apps, SSE streaming, server-side memory)
status: in-review
phase: 5
depends_on: [LLMM-003, LLMM-004]
---

# LLMM-020 — Dify adapter (chat/agent apps, SSE streaming, server-side memory)

## Context
First-class Dify preset (fast-follow roadmap item in `docs/implementation/README.md`).
Dify (dify.ai / self-hosted langgenius/dify) exposes one chat API for its Chatbot, Agent,
and Chatflow app types: `POST {base}/chat-messages` with `response_mode: "streaming"`
(blocking mode is **not supported for Agent apps**, so the adapter is streaming-only).
Replies stream as WHATWG SSE with named `event:` types — `agent_message` carries answer
deltas for Agent apps, `message` for Chatbot/Chatflow; handling both makes one preset
cover all three app types. Dify owns conversation memory server-side: the first turn
returns a `conversation_id` on every stream event, and echoing it back continues the
conversation → stateful (`supports_memory_scope = True`), LangGraph-style persistence.
Tools are configured inside the Dify app; the API has no client-tool passthrough →
`supports_ha_tools = False`. API reference: https://docs.dify.ai/api-reference/chats/send-chat-message
and `web/app/components/develop/template/template_chat.en.mdx` in langgenius/dify.

**Templates:** LangGraph (`backends/langgraph.py`) for the session-key → server-ID
persistence skeleton (in-memory map for `conversation` scope, `helpers.storage.Store`
for `device`/`agent` scopes, transparent recreate on expiry); converse
(`backends/converse.py`) for named-SSE-event dispatch via `_sse.async_iter_sse`.

## Scope
**In:**
- `custom_components/llm_middleman/backends/dify.py` implementing `BackendAdapter`;
  `backend_type = "dify"`; `supports_ha_tools = False`; `supports_memory_scope = True`;
  registered in `BACKEND_TO_CLS`.
- Connection form: `CONF_BASE_URL` (required; store the API root *including* `/v1` after
  normalization — Dify Cloud is `https://api.dify.ai/v1`, self-hosted `http(s)://host/v1`;
  accept both the root and `/v1` forms by stripping a trailing slash and appending `/v1`
  when absent) + `CONF_API_KEY` (required — Dify app keys are mandatory, `app-…`).
- `async_validate_connection`: `GET {base}/info` with the bearer key; 401/403 →
  `BackendAuthError`; network errors / non-2xx → `BackendConnectionError`. When the
  response carries `"mode"` and it is not a chat-type app (`chat`, `agent-chat`,
  `advanced-chat`) → `BackendConnectionError` (wrong app type, e.g. workflow/completion).
- `stream_turn`: `POST {base}/chat-messages` with
  `{"query": user_input.text, "inputs": {}, "response_mode": "streaming", "user": <stable id>,
  "conversation_id": <mapped id or omitted>, "auto_generate_name": false}`.
  Parse SSE via `_sse.async_iter_sse`; role-first delta then `{"content": answer}` for each
  `agent_message`/`message` event; capture `conversation_id` from the first event carrying
  it and persist keyed by `ctx.memory_key`; `message_end` → return; `error` event →
  `BackendStreamError` (stream stays HTTP 200 on in-stream failure); `ping`,
  `agent_thought`, `message_file`, `message_replace`, `tts_message*` → ignored
  (debug-log `agent_thought`/`message_replace`).
- Stale-conversation recreate: echoing an expired/deleted `conversation_id` → HTTP 404
  (`conversation_not_exists`) *before* the stream starts → drop the mapping and retry once
  without `conversation_id` (mirrors LangGraph's 404 recreate).
- Best-effort stop: on generator cancellation mid-stream, fire-and-forget
  `POST {base}/chat-messages/{task_id}/stop` with the captured `task_id`.
- Config flow step + schema, `_BACKEND_TITLES`, `translations/en.json` + `strings.json`
  (`config.step.dify`, `selector.backend_type.options.dify`), diagnostics redaction
  (verify `CONF_BASE_URL`/`CONF_API_KEY` already covered by `TO_REDACT`), README preset
  table row, `tests/test_diagnostics.py` parametrization, new `tests/backends/test_dify.py`.

**Out:**
- HA tools (no API passthrough — tools live in the Dify app).
- Forwarding the per-agent `CONF_PROMPT` (no system-prompt field in the API; the Dify app
  owns its prompt — same stance as converse; document in the step description).
- `inputs` variable mapping, file/vision upload, feedback/suggested-questions endpoints,
  TTS audio events (HA owns TTS), conversation list/rename APIs.

## Implementation notes
- **`user` field is required** by the API ("unique within the application") and
  conversation ownership is scoped to it — reuse of a `conversation_id` must send the same
  `user`. Send the constant `"home-assistant"`; conversation IDs are globally unique so
  scopes cannot collide. (Making it configurable is a future option, not this ticket.)
- Answer deltas: concatenate `answer` fields in arrival order. Emit `{"role": "assistant"}`
  before the first content delta only (role-first invariant, `base.py`).
- `message_replace` (output moderation) cannot un-speak already-streamed text; log it and
  stop emitting further content from the replaced answer if trivially possible — do not
  attempt retroactive replacement.
- Persistence: follow `langgraph.py` exactly — `self._mem` dict for `conversation` scope,
  `Store(hass, STORAGE_VERSION, f"{DOMAIN}.dify.{slugify(discriminator)}")` for
  `device`/`agent` scopes, `asyncio.Lock` around load/save. Unlike LangGraph the server ID
  is only known *mid-stream* (first event), so persist after capture, not before POST.
- Errors: pre-stream non-2xx → read the JSON body `code`/`message` for the log;
  401/403 → `BackendAuthError`; 404 `conversation_not_exists` → recreate path (once);
  anything else → `BackendConnectionError`. In-stream `event: error` payload is
  `{status, code, message}` → `BackendStreamError` including `code`.
- Const keys: `BACKEND_DIFY = "dify"` only — `CONF_BASE_URL`/`CONF_API_KEY` exist.

## Acceptance criteria
- [x] `DifyAdapter(BackendAdapter)` with `backend_type = "dify"`,
      `supports_ha_tools = False`, `supports_memory_scope = True`, in `BACKEND_TO_CLS`.
- [x] Request POSTs `{base}/chat-messages` streaming-only with required `query`/`user`,
      omitting `conversation_id` on first turn, echoing the persisted one after.
- [x] Deltas stream from both `agent_message` and `message` events, role-first;
      `message_end` terminates; `ping`/`agent_thought`/`message_file`/`tts_*` ignored.
- [x] `conversation_id` captured mid-stream and persisted per `ctx.memory_key`
      (in-memory for `conversation` scope, `Store` for `device`/`agent`).
- [x] Stale ID → pre-stream 404 → mapping dropped, one retry without `conversation_id`.
- [x] In-stream `error` event → `BackendStreamError`; bad key on validate →
      `invalid_auth`; wrong app mode (workflow/completion) rejected at config time.
- [x] Cancellation fires best-effort `/chat-messages/{task_id}/stop`.
- [x] Config flow step + translations + strings + README row + diagnostics
      parametrization all updated; API key redacted in diagnostics.
- [x] Gates green: `just test`, `just lint`, `just fmt-check`, `just typecheck`.

## Verification
`tests/backends/test_dify.py` driving raw SSE bytes through the real parser (conftest
`sse_bytes`/`chunk_bytes` helpers, boundaries split mid-event), mirroring
`test_langgraph.py` structure: streaming happy path (agent_message + message variants),
conversation_id capture/echo/persistence per scope (`hass_storage` fixture), 404 recreate
retry-once, in-stream error event, ping/thought/file events ignored, request-shape
assertions (streaming mode, user constant, auto_generate_name false), stop-on-cancel,
validate-connection matrix (200 chat mode / 200 workflow mode / 401 / network error).
Config-flow test for the dify step. `tests/test_diagnostics.py` parametrized over the new
backend. Run the full gate; record baseline → delta.

### Verification evidence (executed)
`tests/backends/test_dify.py` (24 tests) drives raw SSE bytes through the real `_sse`
parser via the conftest harness (mid-event/CRLF splits) and covers: streaming happy path
for both `agent_message` and `message` events × `\n`/`\r\n`; `message_end` termination
(content after it dropped); ignored events (ping/agent_thought/message_file/tts) produce no
deltas; in-stream `error` → `BackendStreamError`; request shape (streaming mode,
`user="home-assistant"`, `auto_generate_name=false`, `inputs={}`, `conversation_id` omitted
on first turn + Bearer header); `conversation_id` capture + echo on turn 2; conversation
scope not persisted; device scope persisted to `Store` + reload across a fresh adapter
instance ("restart"); stale-id pre-stream 404 `conversation_not_exists` → dropped + one
retry without id; pre-stream 401 → `BackendAuthError` and 500 → `BackendConnectionError`;
best-effort stop fired on mid-stream `aclose()` (and *not* fired before a `task_id` was
seen); validate matrix (200 chat/agent-chat/advanced-chat pass, 200 workflow →
`BackendConnectionError`, 401 → `BackendAuthError`, transport error →
`BackendConnectionError`). Plus `tests/test_config_flow.py::test_dify_appends_v1_suffix`
(4 params: host, host/, host/v1, host/v1/ all → `…/v1`) and `tests/test_diagnostics.py`
parametrized over `BACKEND_DIFY`.

- `uv run pytest -q` → `250 passed` (baseline `221 passed` → +29 new; no existing test
  regressed).
- `uv run basedpyright` (strict) → `0 errors, 0 warnings, 0 notes`.
- `uv run ruff check .` → `All checks passed!`; `uv run ruff format --check .` →
  `32 files already formatted`.

## Risks / open questions
- **Older self-hosted Dify versions** may predate `GET /info` without a `user` param; the
  E2E rig / a live install is the real validation (same stance as other presets).
- `message_replace` semantics under streaming TTS are inherently lossy (can't un-speak);
  logged, not solved.
- The constant `user` identifier groups all HA conversations under one Dify end-user; if
  someone needs per-HA-user attribution later it becomes a config option (new ticket).
