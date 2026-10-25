---
id: LLMM-025
title: n8n + converse tier-1 (real reply shapes, segment separators, safe webhook probe, terminal-state discipline, tool activity)
status: in-review
phase: 5
depends_on: [LLMM-009, LLMM-012]
---

# LLMM-025 — n8n + converse tier-1

## Context
LLMM-021 (Dify) and LLMM-022 (LangGraph) set the "tier-1" bar for a preset: every documented
reply shape works, a dropped stream is never passed off as complete, the setup probe catches
the common misconfigurations, backend tool activity reaches the Assist chat, and the adapter
is pinned by real wire captures. A 2026-10-25 audit of the n8n and converse presets against
`n8nio/n8n:2.44.6` (released 2026-10-22) and the converse contract
(`docs/external-agent-handoff/implementation-brief.md` §4, `scripts/e2e/converse_sse_stub.py`)
found that n8n setups the n8n docs recommend fall back or speak raw JSON, and that converse
has gaps in its terminal-state handling. The keepalive sniff and the read timeout were already
fixed in #110 (`.wiki/n8n-keepalive-and-chat-trigger-probe.md`,
`.wiki/agent-timeout-is-max-silence.md`).

Sources: n8n 2.44.6 dist in the image (`webhooks/webhook-request-handler.js`,
`errors/response-errors/webhook-not-found.error.js`, `services/webhook.service.js`,
`@n8n/n8n-nodes-langchain/dist/utils/agent-execution/buildSteps.js`), the `@n8n/chat` client
(`packages/frontend/@n8n/chat/src/plugins/chat.ts` and `api/message.ts` at `n8n@2.44.6`), and live
captures under `tests/backends/fixtures/n8n/`.

## Scope
**In** (`backends/n8n.py`, `backends/converse.py`, the n8n error mapping in `config_flow.py`,
`strings.json` + `translations/en.json`, their tests, `tests/backends/fixtures/{n8n,converse}/`,
the converse spec docs and stub, README, CHANGELOG):

n8n
1. A Respond-to-Webhook **Text** reply is served as `text/html` and was rejected as a proxy error
   page, so a valid setup always fell back. Only a real HTML document (`<!doctype` / `<html`)
   counts as an error page now; any other `text/*` body is the reply.
2. Webhook **All Entries** / Respond **All Incoming Items** returns a JSON array, which was
   rejected. Arrays are accepted: every item's reply field is joined in order.
3. Respond-to-Webhook **JSON** / **First Incoming Item** in streaming mode sends the JSON as an
   `item.content` string, which was spoken verbatim (`{"output":"json reply"}`). An item whose
   content is a JSON object or array carrying the reply field is unwrapped. Anything else is
   still spoken as text.
4. Several `begin`/`end` segments were joined with no separator ("First part.Second part.").
   A newline now separates segments.
5. The setup probe only checked the URL's shape. It now also rejects `/webhook-test/` URLs and
   sends a run-free registration probe (see the notes), so an inactive workflow is a form error.
6. An `error` chunk with `content: null` logged `None`. The message now names the failing node
   from the chunk metadata.
7. AI Agent `intermediateSteps` in a blocking reply are shown in the Assist chat as external tool
   calls.

converse
8. `done` with no text and no deltas spoke the untranslated `ERROR_MESSAGE` without
   `ctx.fallback`. It now goes through the entity guard's fallback (translated, intent error).
9. A stream that ended without `done` (or `error`) counted as complete, while the docstring
   claimed it fell back. It now raises like Dify does (a dropped stream is not complete) and the
   docstring is fixed.
10. `tool_activity` events were dropped. They now reach the Assist chat as external tool calls.
11. The setup probe ignored the status, so a bad token passed. 401/403 now map to `invalid_auth`.
12. Spec and stub: an optional keepalive rule, the `[timestamp] ` prefix on `text`, the
    `tool_activity` ordering rule, and the stale "five presets" fixed. The stub gains error-event
    `code`, truncation and `tool_activity` cases. These are optional, additive contract notes only;
    the wire contract is unchanged.

**Out:** HA tools for these presets; n8n `loadPreviousSession`; n8n Chat Trigger "User Auth";
an n8n node filter (like LangGraph's `response_node_filter`); tool activity in n8n streaming
mode (2.44.6 streams no tool chunks); a separate "max silence" option.

## Implementation notes
- **Mode sniff** (`n8n._sniff_mode`): html document → ndjson first line / ndjson content-type →
  `text/*` → blocking JSON. A proxy error with an error status never gets here
  (`_post_checked` raises on ≥ 400). The HTML check stays for 200 pages, such as a login portal
  in front of n8n, which are full documents. The Text reply is not a document.
- **Reply extraction** (`n8n._reply_from_json`) is shared by the blocking body and streamed
  JSON-string items: object → `output_field` → `text` fallback, plus `continueConversation`;
  array → the replies of its object items, joined with `"\n"`. n8n's own chat client has no array
  handling (it reads `output ?? text ?? message`, otherwise it stringifies the response). Joining
  keeps every reply the workflow produced instead of silently dropping later items. The newline
  matches the segment separator and the entity guard's partial-reply separator.
- **Segment separator**: after an `end`, if content was already emitted, the next item content
  is prefixed with `"\n"`. The `@n8n/chat` client renders one message per `begin`/`end`, so each
  segment is a separate utterance.
- **Webhook probe** (`N8nAdapter.async_validate_connection`): `OPTIONS` with an `Origin` header,
  and never `GET`. In n8n, `OPTIONS` + `Origin` resolves the path's registered methods and returns
  before `executeWebhook`, so it cannot start a run. That holds for a Chat Trigger too, so both
  targets are probed. This deliberately deviates from the audit's "GET plain Webhooks, skip Chat
  Trigger". A GET would start a run on a Webhook configured to also accept GET, and OPTIONS covers
  the default Chat Trigger target as well. Verified live on 2.44.6, where executions stayed at
  13 → 13 across four probes. Mapping:
  - transport failure → `cannot_connect`
  - 401/403 → `invalid_auth`
  - 404 "is not registered", or a 2xx whose `Access-Control-Allow-Methods` lacks `POST` →
    `webhook_not_registered`
  - anything else is accepted, because a reverse proxy may answer `OPTIONS` itself

  `Access-Control-Request-Method` is never sent: n8n 2.44.6 answers 500 for an unknown path with
  it. A URL with a `/webhook-test/` path is rejected before any request (`webhook_test_url`).
  At setup, a not-registered webhook is `ConfigEntryNotReady`, which retries. That is correct,
  because n8n registers webhooks only after its workflows activate.
- **Tool activity, n8n**: in the blocking body, `intermediateSteps[]`
  (`{action: {tool, toolInput, toolCallId, …}, observation}`; `observation` is a JSON string,
  and `{"error": …}` marks a failure) → `external_tool_call_delta` / `external_tool_result_delta`,
  emitted before the reply block, so the stream never ends on a tool result. Shape from
  `buildSteps.js` (2.44.6). It appears only with the Agent's "Return Intermediate Steps" option.
- **Tool activity, converse**: `tool_activity` events are buffered and flushed as external tool
  deltas right before the first spoken text (a `text_delta`, or `done.text`). Activity that
  arrives after text has started, or with no text at all, goes to the conversation trace
  (`converse_tool_activity`) instead. Placing it there would either end the stream on a tool
  result, which re-enters the entity's tool loop and re-POSTs the turn, or split the reply so
  the spoken result loses its first part. The spec's own example sends `tool_activity` after the
  text, so it must not fail the turn. The spec now says to send it before the text to have it
  shown. Call id: the optional `id` field when present, else `converse-tool-N`. The result is
  `{"status": …}`; any status other than `ok` is an error.
- **converse terminal states**: the only success terminal is `done`. EOF without it raises
  `BackendStreamError`, including when nothing was streamed: a silent EOF used to yield nothing,
  and it is now a dropped stream too, like Dify. After partial text, the guard appends the
  fallback on a new line. `done` with nothing to say yields nothing, so the guard's silent-end
  fallback applies. An `error` event without a `code` logs `no code`, never `None`.
- Fixtures: n8n captures replayed through the real adapter; converse fixtures are labelled
  stub-derived (`scripts/e2e/converse_sse_stub.py` is the contract's reference backend).

## Acceptance criteria
- [x] 1 — Text reply (`text/html`) spoken; HTML documents still rejected.
- [x] 2 — All Entries / All Incoming Items arrays spoken (joined).
- [x] 3 — JSON-string streamed items unwrapped; non-JSON / no-field items spoken verbatim.
- [x] 4 — Segments separated by a newline.
- [x] 5 — Run-free OPTIONS probe; `webhook_not_registered` / `webhook_test_url` form errors; 401/403 → `invalid_auth`.
- [x] 6 — Null error content logs the failing node, never `None`.
- [x] 7 — Blocking `intermediateSteps` surfaced as external tool calls before the reply.
- [x] 8 — converse empty `done` → guard fallback (translated, `ctx.fallback`).
- [x] 9 — converse EOF without `done` raises; docstring matches.
- [x] 10 — converse `tool_activity` surfaced (pre-text) or traced (post-text); never ends on a tool result.
- [x] 11 — converse probe: 401/403 → `invalid_auth`.
- [x] 12 — Spec/stub updated, additive only.
- [x] Fixtures captured from n8n 2.44.6 and replayed in tests.
- [ ] Owner-run live rows against a real n8n with an AI Agent (NOT RUN).

## Verification
Agent-run (devcontainer, 2026-10-25), on `feat/llmm-025-n8n-converse-tier1` off `d48e88a`:
- Baseline before any edit: `uv run pytest -q` → `801 passed`.
- Red first: with only `N8nWebhookError` added, the new n8n tests showed 30 failing (items 1–7,
  probe). The new converse tests showed 14 failing (items 8–11). Two pre-existing tests pinned
  the old behaviour and were updated to the new contract: `test_multiple_begin_end_cycles`
  (item 4) and the converse silent-EOF / empty-`done` tests (items 8–9).
- Live, n8nio/n8n:2.44.6 (container `llmm-025-n8n`, removed after): the real `N8nAdapter`
  spoke all ten captured workflows correctly. `stream-err` falls back naming node `'C'`. Probes:
  - Webhook and Chat Trigger: OK
  - unregistered path: `webhook_not_registered`
  - deactivated workflow: answers `Allow-Methods: OPTIONS` only, so `webhook_not_registered`
  - `/webhook-test/`: `webhook_test_url`

  Executions went 13 → 23 across ten turns plus four probes (and 13 → 13 across four probes
  alone), so the probe started no run.
- Live, converse stub (local, port 18099, stopped after): all eight stub cases behave as
  specified through the real `ConverseAdapter` (cut → raises after the partial text; tool → two
  external calls; late → traced; empty → nothing yielded).
- Gate, locked HA 2026.10.3 (all rc=0):
  - `uv lock --check`
  - `uv run pytest -q` → `870 passed`
  - `uv run coverage run -m pytest -q && uv run coverage report` → TOTAL 98% (n8n.py 96%,
    converse.py 98%)
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run basedpyright` → 0 errors
  - `uv run pre-commit run --all-files`
- CI floor leg (throwaway detached worktree, HA 2026.7.0 overlay per `lint.yml`, removed
  after): `uv run --no-sync pytest -q tests/` → `862 passed, 8 skipped`, rc=0. The skips are all
  HA-version gates; two new ones are the tool-result error flag (HA ≥ 2026.10).
- Mutation checks: each of these turns 2–3 backend tests red:
  - dropping the converse EOF raise or the trace
  - dropping the n8n separator, text mode or JSON unwrap
  - breaking the not-registered mapping

Owner-run (NOT RUN): an n8n AI Agent with Return Intermediate Steps (tool calls in the Assist
chat), setup against an inactive workflow on a real HA, and the converse stub rows in
`scripts/e2e/README.md`.

## Risks / open questions
- The OPTIONS probe is verified on n8n 2.44.6 only. Older n8n that 404s an unknown OPTIONS path
  maps to the same error, and a reverse proxy that answers OPTIONS itself makes the probe a no-op.
  It never blocks setup on an unknown answer.
- `intermediateSteps` comes from n8n source, not a capture: it needs a chat model. The owner-run
  rows should confirm it.
- Unwrapping a JSON-string item could in theory catch an LLM that streams one complete
  `{"output": …}` object as a single token chunk. That is unlikely, and the result would still be
  the intended reply.
