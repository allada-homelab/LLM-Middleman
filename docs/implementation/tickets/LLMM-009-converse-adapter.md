---
id: LLMM-009
title: Custom `/v1/converse` adapter (v0 contract through new parser/guard)
status: todo
phase: 2
depends_on: [LLMM-002, LLMM-003, LLMM-004]
---

# LLMM-009 — Custom `/v1/converse` adapter (v0 contract through new parser/guard)

## Context
The v0 shim's bespoke `/v1/converse` SSE contract survives as one preset (plan.md §Context
"User decisions", §Architecture `backends/converse.py`, §Per-connector configuration matrix
**Custom `/v1/converse`** row, §Streaming parsers `text_delta`/`done`/`error`). This ticket
**ports v0 `conversation.py:104-211` semantics** into a `BackendAdapter`, but routes the
byte stream through the new spec-compliant `_sse.py` reader (LLMM-002) and the entity's
never-hangs guard (LLMM-005) instead of v0's per-line parser and narrow `except`. It also
finally wires `done.continue_conversation` (a documented contract field v0 ignored — see
plan §Follow-up listening and research-3 "Doc-vs-code gap"). This is the **reference
adapter**: cleanest fit for a text-only voice turn (text in, streaming text out, no tool
plumbing), and the canonical internal delta shape the other presets normalize into.

## Scope
**In:**
- `custom_components/llm_middleman/backends/converse.py` implementing the `BackendAdapter`
  ABC; `backend_type = "converse"`; `supports_ha_tools = False`; stateful (the backend
  owns history, keyed on the forwarded session key).
- `stream_turn(self, chat_log, user_input, ctx: TurnContext)`: `POST {base_url}/v1/converse`
  with the v0 body shape, consuming the response via `_sse.py`, dispatching on the named
  events `text_delta` / `done` / `error`.
- Send only the **new turn** + `ctx.memory_key` (stateful adapter — no history replay);
  backend keys its own state on that value via the request's `conversation_id` field.
- Wire `done.continue_conversation` → set `ctx.continue_conversation = True` so the entity
  ORs it into its `ConversationResult` (finally honoring the field v0 dropped).
- Docstring / module note: **this preset is the reference adapter** and documents the
  internal canonical delta shape.

**Out:**
- HA tools (the backend owns its own tools server-side — text-only passthrough).
- The `ConversationResult` construction itself and the memory_scope key derivation live in
  the entity (**LLMM-005**); this ticket consumes `ctx.memory_key` and sets
  `ctx.continue_conversation` — it does not build the result or derive the key.
- v0→v1 config-entry migration (v0 entry → converse parent + subentry) — **LLMM-013**.
- Parent connection form (`base_url` + bearer `token`) — **LLMM-006**.

## Implementation notes
- **Port source:** v0 `custom_components/llm_middleman/conversation.py:104-211`. Reuse:
  - Request body (`:110-116`): `{"conversation_id": <session_key>, "text": user_input.text,
    "language": user_input.language}` plus `"device_id"` only when truthy.
  - URL build (`:122`): `base_url.rstrip("/") + "/v1/converse"` (`CONVERSE_PATH` in
    `const.py:12`).
  - Headers (`:118-120`): `{"Accept": "text/event-stream"}` + `Authorization: Bearer
    <token>` only when `CONF_TOKEN` set.
  - Event handling (`:176-199`): `text_delta` → role-first `{"role":"assistant"}` then
    `{"content": payload["delta"]}`; `done` with no prior delta → emit `payload.get("text")
    or ERROR_MESSAGE`; `done` after deltas → stop (deltas are authoritative,
    `done.text` discarded — matches v0 behavior, research-3 confirms).
- **Replace, do NOT port, the parser:** v0 parsed SSE line-by-line (a listed v0 defect,
  plan §Verified constraints). Consume events from `_sse.py` (LLMM-002), which buffers
  consecutive `data:` lines and dispatches on the blank line, handles CRLF / `:` comments,
  and re-raises oversized-line `ValueError` as a typed `BackendStreamError`.
- **Error/guard:** on an `error` event, log code+message (redacted) and raise
  `BackendStreamError` (or yield the canonical fallback) — let the entity's `_guarded()`
  wrapper (LLMM-005) produce the fallback `AssistantContent`. Do **not** re-implement v0's
  narrow `except (TimeoutError, aiohttp.ClientError)`; the guard catches `Exception`
  broadly (v0's `ValueError`/`UnicodeDecodeError` holes).
- **Session key:** the entity derives the key (plan §Conversation continuity) and passes it
  as `ctx.memory_key`; converse forwards it verbatim as the request `conversation_id`. No
  inline derivation here — the adapter never reads `CONF_MEMORY_SCOPE`/`device_id` itself.
- **continue_conversation seam:** on the terminal `done` event, if
  `done.continue_conversation` is true, set `ctx.continue_conversation = True`. The entity ORs
  `ctx.continue_conversation` into its `ConversationResult` (LLMM-005). This is the
  `TurnContext` contract from LLMM-003 — no separate method/attribute on the adapter.
- **Const keys:** `CONF_TOKEN`, `CONF_URL`/`CONF_BASE_URL`, `CONVERSE_PATH`, `ERROR_MESSAGE`
  already exist in `const.py`; add `BACKEND_CONVERSE = "converse"`.

## Acceptance criteria
- [ ] `ConverseAdapter(BackendAdapter)` with `backend_type = "converse"`,
      `supports_ha_tools = False`, registered in `BACKEND_TO_CLS`.
- [ ] `stream_turn` POSTs the v0 body to `{base_url}/v1/converse` (bearer token when set)
      and streams `text_delta.delta` as role-first `AssistantContentDeltaDict` deltas.
- [ ] A `done` event with no prior delta emits `done.text` (or `ERROR_MESSAGE`); a `done`
      after deltas terminates without duplicating text.
- [ ] An `error` event surfaces as a `BackendStreamError` (guard → fallback), never a hang.
- [ ] `done.continue_conversation` (when true) sets `ctx.continue_conversation = True`.
- [ ] The request `conversation_id` is `ctx.memory_key` (derivation lives in the entity).
- [ ] Module documents that this is the reference adapter.
- [ ] Gates green: `just check` + `just typecheck`.

## Verification
Write `tests/backends/test_converse.py` driving **raw bytes** through `_sse.py` + adapter
(not v0's `_FakeContent` pre-split lines — plan §Verification):
- **Happy path:** bytes for `event: text_delta\r\ndata: {"delta":"Hi"}\r\n\r\n` then
  `event: done\r\ndata: {"text":"Hi","continue_conversation":true}\r\n\r\n`, with chunk
  boundaries split mid-frame. Assert deltas `[{"role":"assistant"},{"content":"Hi"}]` and
  that the adapter set `ctx.continue_conversation is True`.
- **Multi-line `data:`:** two consecutive `data:` lines in one event are concatenated by
  the reader before dispatch.
- **done-without-delta:** only `event: done\ndata: {"text":"All set"}` → emits
  `{"content":"All set"}`.
- **error event:** `event: error\ndata: {"code":"x","message":"boom"}` → raises
  `BackendStreamError`; assert the guard path yields the fallback (via the entity test or a
  direct assertion on the raised type).
- **silent EOF:** stream ends with no `done`/`error` → terminates; guard supplies fallback.
- **continue absent:** `done` without `continue_conversation` → `ctx.continue_conversation`
  stays `False`.
Run `just check` + `just typecheck`; record delta vs baseline.

## Risks / open questions
- Plan §Implementation-time checkpoints: confirm `async_add_delta_content_stream` +
  `async_get_result_from_chat_log` mechanics against the pinned HA source (the
  `continue_conversation` computed property was already verified against the installed HA —
  §Follow-up listening).
