---
id: LLMM-011
title: LangGraph adapter (threads, messages-tuple, Store-persisted mapping)
status: todo
phase: 2
depends_on: [LLMM-002, LLMM-003, LLMM-004]
---

# LLMM-011 — LangGraph adapter (threads, messages-tuple, Store-persisted mapping)

## Context
The LangGraph Platform preset — the **stateful-thread** axis of the adapter interface
(plan.md §Architecture `backends/langgraph.py`, §Per-connector configuration matrix
**LangGraph** row, §Streaming parsers "messages-tuple filtered by `metadata.langgraph_node`",
§Conversation continuity "maps key → `thread_id`", §Serialization). Same wire API for
`langgraph dev` / self-hosted / cloud. Text-only passthrough — the graph owns its tools
server-side, so `supports_ha_tools = False`. This is the researcher's **least-confident
adapter**: the exact `messages-tuple` frame shape and terminal event names MUST be verified
against a live `langgraph dev` capture before the parser is finalized (plan
§Implementation-time checkpoints — made an acceptance criterion below).

## Scope
**In:**
- `custom_components/llm_middleman/backends/langgraph.py` implementing the `BackendAdapter`
  ABC; `backend_type = "langgraph"`; `supports_ha_tools = False`; stateful.
- `async_validate_connection`: `GET {base_url}/ok`, fallback `POST {base_url}/assistants/search`;
  auth `x-api-key`; raise on failure.
- `stream_turn(self, chat_log, user_input, ctx: TurnContext)`: ensure a `thread_id` for
  `ctx.memory_key` (create + persist), then
  `POST {base_url}/threads/{thread_id}/runs/stream` with `stream_mode=messages-tuple`,
  sending only the **new turn**; parse the SSE stream via `_sse.py`; extract token deltas
  and filter by `metadata.langgraph_node`; terminate on `event: end` / `event: error`.
- `stateless_runs` toggle → `POST {base_url}/runs/stream` (no thread) instead.
- Thread map (`session_key → thread_id`) persisted via `homeassistant.helpers.storage.Store`
  for `device`/`agent` scopes; `conversation`-scoped mappings stay in-memory.
- Options wired: `assistant_id` (parent, default `"agent"`), `input_messages_key`
  (default `messages`), `response_node_filter` (optional), `stateless_runs`, optional
  system prompt, `timeout`.

**Out:**
- HA tools (graph tools live server-side).
- Parent connection form + subentry option schema — LLMM-006/LLMM-007.
- Memory-scope key derivation itself lives in the entity (LLMM-005); this ticket consumes
  `ctx.memory_key` and maps it to a `thread_id`.

## Implementation notes
- **Endpoints (LangGraph Platform API):**
  - Create thread: `POST {base_url}/threads` (empty/`{}` body) → `{"thread_id": …}`.
  - Streamed run: `POST {base_url}/threads/{thread_id}/runs/stream` with body
    `{"assistant_id": <assistant_id>, "input": {<input_messages_key>: [<new message(s)>]},
    "stream_mode": "messages-tuple"}`. Stateless variant: `POST {base_url}/runs/stream`
    with the same body minus the thread path.
  - Validate: `GET {base_url}/ok`; if that 404s, `POST {base_url}/assistants/search`
    (body `{}`), Bearer/`x-api-key` as configured.
- **Auth:** `x-api-key: <api_key>` header (NOT `Authorization: Bearer` — deployment sends
  it as `x-api-key` per plan matrix). `api_key` optional (self-hosted often none).
- **Input shape:** graphs define their own input schema, hence `input_messages_key`
  (default `messages`). Send only the new user turn: `{"role":"user","content": text}`.
  When a system prompt is configured AND the graph uses `MessagesState`, prepend a
  `{"role":"system","content": prompt}` message (optional; documented as best-effort).
- **Streaming (messages-tuple):** consume SSE via `_sse.py`. In `messages-tuple` mode each
  `data:` frame carries a tuple `[message_chunk, metadata]`; extract the token text from
  the message chunk's `content` and the node name from `metadata.langgraph_node`. Emit
  role-first `{"role":"assistant"}` then `{"content": token}`. **Filter:** when
  `response_node_filter` is set, only emit tokens whose `metadata.langgraph_node` matches
  (so intermediate/tool-node chatter isn't spoken). Terminal events: `event: end`
  (success) / `event: error` (raise `BackendStreamError` → guard fallback). **VERIFY the
  exact tuple/field shape and event names against a live capture — see checkpoint.**
- **Thread mapping + persistence** (`session_key` below is `ctx.memory_key`, derived by the
  entity):
  - `conversation` scope: in-memory `dict[session_key, thread_id]` on the adapter instance
    (HA rotates `conversation_id` after the 5-min TTL, so a stale key just makes a new
    thread — no persistence needed).
  - `device`/`agent` scope: persist `session_key → thread_id` via
    `helpers.storage.Store(hass, STORAGE_VERSION, f"{DOMAIN}.langgraph.{entry_id}")` so
    long-lived threads survive HA restarts. Load on adapter init, save on new-thread
    creation.
  - If a mapped `thread_id` is rejected (404/deleted server-side), create a fresh thread
    and update the map — fail soft, never hang.
- **Serialization:** any HA object serialized into a request body uses
  `json.dumps(..., default=str)` (plan §Serialization; webhook-conversation issue #40
  class of crash).
- **Const keys:** add `CONF_ASSISTANT_ID` (default `"agent"`), `CONF_INPUT_MESSAGES_KEY`
  (default `messages`), `CONF_RESPONSE_NODE_FILTER`, `CONF_STATELESS_RUNS`,
  `BACKEND_LANGGRAPH = "langgraph"`, `STORAGE_VERSION`.

## Acceptance criteria
- [ ] `LangGraphAdapter(BackendAdapter)` with `backend_type = "langgraph"`,
      `supports_ha_tools = False`, registered in `BACKEND_TO_CLS`.
- [ ] `async_validate_connection` tries `GET /ok` then `POST /assistants/search` with
      `x-api-key`; raises on failure.
- [ ] `stream_turn` creates/reuses a `thread_id`, POSTs
      `/threads/{id}/runs/stream` with `stream_mode=messages-tuple` and `assistant_id`,
      sending only the new turn; `stateless_runs` uses `/runs/stream`.
- [ ] Token deltas extracted from messages-tuple frames, filtered by
      `metadata.langgraph_node` when `response_node_filter` is set; role-first delta.
- [ ] Terminal `end`/`error` handled; a rejected `thread_id` transparently re-creates.
- [ ] `device`/`agent`-scope thread map persists across restarts via `Store`;
      `conversation`-scope stays in-memory.
- [ ] **The messages-tuple frame shape and terminal event names are verified against a live
      `langgraph dev` capture, and the captured frames are checked into the test fixtures**
      (plan §Implementation-time checkpoints).
- [ ] Gates green: `just check` + `just typecheck`.

## Verification
- **Capture first (blocking the parser):** run a sample graph under `langgraph dev`, POST a
  `/threads/{id}/runs/stream` with `stream_mode=messages-tuple`, and capture the raw SSE
  bytes (curl `--no-buffer` or the entity's aiohttp log). Save the capture as a test fixture
  and derive the parser from the **real** frame shape — do not finalize from memory.
- Write `tests/backends/test_langgraph.py` driving the captured **raw bytes** through
  `_sse.py` + adapter (split chunks/CRLF — plan §Verification):
  - **Happy path:** captured token frames → role-first deltas concatenate to the reply;
    stops on `event: end`.
  - **Node filter:** frames from two `langgraph_node` values with `response_node_filter`
    set to one → only that node's tokens are emitted.
  - **error event:** `event: error` → `BackendStreamError` (guard fallback).
  - **Thread reuse:** two `stream_turn` calls with the same `device`-scope key reuse the
    same `thread_id`; a new key creates a new thread.
  - **Persistence:** a fake `Store` round-trips the map; after "restart" (new adapter
    instance) a `device`-scope key resolves to the persisted `thread_id`.
  - **Stateless toggle:** `stateless_runs=True` → POSTs `/runs/stream`, never `/threads`.
- Run `just check` + `just typecheck`; record delta vs baseline. Live E2E against
  `langgraph dev` is covered by LLMM-018.

## Risks / open questions
- **Least-confident item (plan-flagged):** the `messages-tuple` frame is graph-defined and
  the terminal event names are version-specific — the live-capture acceptance criterion
  exists precisely to de-risk this. If the capture disagrees with the notes above, the
  capture wins; update the ticket in-PR.
- `GET /ok` availability varies by deployment (dev vs cloud vs self-hosted) — hence the
  `/assistants/search` fallback; confirm both against the target during E2E.
- Persisted thread IDs can outlive server-side threads (TTL/GC) — the re-create-on-404 path
  must be exercised, or `device`/`agent` scope silently breaks after server cleanup.
