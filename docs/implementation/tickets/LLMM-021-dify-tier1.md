---
id: LLMM-021
title: Dify tier-1 (spec-exact streaming, stop/recreate hardening, inputs, identity, tool activity)
status: in-progress
phase: 5
depends_on: [LLMM-020]
---

# LLMM-021 — Dify tier-1

## Context
The owner runs Dify in production (a "New Jarvis" agent) through the LLMM-020 preset, which
supersedes it here. A review against the official Dify Service API OpenAPI spec
(https://docs.dify.ai/en/api-reference/openapi_service.json, index at
https://docs.dify.ai/llms.txt; fetched 2026-10-24) found real bugs and missing
capabilities. Spec sections cited below: **AppInfoResponse.mode**, **POST /chat-messages**
(`ChatRequest`, the `text/event-stream` "Events by app type" + "Common fields" description,
400/404/429 error codes), **POST /chat-messages/{task_id}/stop**, **GET /parameters**
(`user_input_form`), stream event schemas (`StreamEventChatMessageReplace`,
`StreamEventChatMessageEnd` + `Usage`, `StreamEventChatReasoningChunk`,
`StreamEventWorkflowPaused`, `StreamEventChatAgentThought`).

## Scope
**In** (`backends/dify.py`, Dify parts of `config_flow.py` / `const.py` / `strings.json` /
`translations/en.json`, `tests/backends/test_dify.py`, Dify tests in `tests/test_config_flow.py`,
owner-run E2E docs/scripts):
1. Accept new-style Agent apps (`mode: agent`, AppInfoResponse.mode); workflow/completion apps
   get a specific `not_chat_app` config-flow error.
2. Capture `task_id`/`conversation_id` from ANY event ("Common fields": all events but `ping`
   carry them, `error` lacks `task_id`; a Chatflow opens with `workflow_started`/`node_*`).
3. Fire the best-effort stop on timeout and on every other exception once a `task_id` is
   known, not only on cancellation (fire-and-forget; the original error still propagates).
4. Recover 400 `conversation_completed` like the stale-conversation 404 (drop mapping, retry
   once without `conversation_id`).
5. Require a terminal event: EOF without `message_end`/`error`/`workflow_paused` →
   `BackendStreamError`. `workflow_paused` (stream ends; the run resumes only via the
   human-input form API) → speak a short notice, trace the pause, do not stop the run.
6. `message_replace`: spoken only when nothing has been spoken yet, else traced + logged;
   later answer chunks dropped.
7. `message_end.metadata.usage` → `chat_log.async_trace({"stats": {input_tokens,
   output_tokens}, "dify_usage": usage})` (core anthropic `_create_token_stats` shape).
8. Inputs: static mapping option + opt-in HA context into declared `ha_*` variables only
   (`GET /parameters`, cached 300 s per entry), incl. the satellite extra prompt kept on the
   chat log for follow-up turns.
9. Option to send the HA user id as `user` (fallback `home-assistant`); the stop sends the
   same `user` (spec: a mismatched user makes stop a silent no-op).
10. Validation also probes `GET /parameters` (`agent_not_published` → own error); the agent
    form lists the app's declared inputs.
11. Chatflow `reasoning_chunk` → `thinking_content` deltas.
12. 429 `too_many_requests`/`rate_limit_error` (and `invalid_param`, `agent_not_published`,
    `not_chat_app`) → named log hint, still fallback. `auto_generate_name` stays `false`.
13. Tool activity (Agent `agent_thought` tool/tool_input/observation; Chatflow tool `node_*`)
    as external tool calls in the Assist chat — **built on the shared helper the LangGraph
    tier-1 branch adds to `backends/_compat.py`; not implemented until that lands on main.**

**Out:** HA tools passthrough (no API for it), forwarding `CONF_PROMPT`, file/vision upload,
feedback/suggested-question endpoints, TTS audio events, submitting human-input forms.

## Implementation notes
- Terminal-state handling lives in `DifyAdapter._run_stream`; ids are captured before
  dispatch so even an `error`-only Chatflow persists its `conversation_id`.
- Stop: `stream_turn` catches `GeneratorExit`/`CancelledError` and `Exception`, calls
  `_stop_started_task(state, user)`, re-raises.
- Pre-stream errors: `_on_error_status` (the `_post_checked` hook) → `_ConversationGone` via
  `_is_stale_conversation` (404 `not_found`/`conversation_not_exists`, 400
  `conversation_completed`), `_ERROR_HINTS` for the warning log.
- New subentry options (`const.py`): `dify_inputs` (ObjectSelector, validated as
  `{non-empty str: str|int|float|bool}`; error `invalid_dify_inputs`), `dify_ha_context`
  (BooleanSelector), `dify_user_from_ha` (BooleanSelector). Only rendered for Dify entries;
  the `dify_inputs` description carries the `{dify_app_inputs}` placeholder.
- Config-flow errors via `DifyAppError(BackendConnectionError).error_key`
  (`not_chat_app`, `agent_not_published`).
- `auto_generate_name: false` kept deliberately: title generation is an extra LLM call per
  new Dify conversation (one per voice command under conversation scope), competing with the
  next turn on single-GPU local models.
- Time context: Dify keeps the entity's `[timestamp] ` query prefix unconditionally (the
  default of `BackendAdapter.wants_time_prefix` from the robustness PR #97). No `ha_*` time
  input is added: the prefix already reaches every app (incl. "New Jarvis") with no app
  changes, while dropping it only for apps that declare a time input would need per-turn
  dynamic gating that the ClassVar flag doesn't support.
- HA APIs verified on both the locked (2026.10.3) and floor (2026.7.0) HA:
  `ChatLog.async_trace`, `ChatLog.extra_system_prompt` (persisted by
  `async_provide_llm_data`), `AssistantContentDeltaDict.thinking_content`.

## Acceptance criteria
- [x] 1 — `agent` mode accepted; workflow/completion → `not_chat_app` form error.
- [x] 2 — ids captured from any event; pre-answer Chatflow cancellation fires stop.
- [x] 3 — stop fires on idle timeout and any exception after a `task_id`; stop failure swallowed.
- [x] 4 — 400 `conversation_completed` dropped + retried once (also for persisted scopes).
- [x] 5 — EOF without a terminal event raises; `workflow_paused` speaks `PAUSED_NOTICE`, traced, no stop.
- [x] 6 — `message_replace` before text spoken; after text traced + warned, never appended.
- [x] 7 — usage traced as `stats` + `dify_usage` (no `stats` when the token split is absent).
- [x] 8 — static inputs sent; HA context only for declared variables; cached with TTL; fetch failure never fails the turn; `max_length` respected.
- [x] 9 — HA-user option with fallback; stop sends the turn's `user`.
- [x] 10 — `/parameters` probed at setup (`agent_not_published` → own error); declared inputs listed in the agent form.
- [x] 11 — `reasoning_chunk` → `thinking_content`.
- [x] 12 — 429/known-code hints logged; `auto_generate_name` decision documented.
- [ ] 13 — tool activity surfaced via the shared `_compat.py` helper (pending the helper on main).
- [ ] Owner-run live E2E rows in `docs/implementation/e2e-results/dify.md` (NOT RUN).

## Verification
Agent-run (devcontainer, 2026-10-24) — commands and results:
- `uv run pytest -q` → `483 passed` (baseline on `4fc5463`: `443 passed`; +40, none regressed).
- `uv run coverage run -m pytest -q && uv run coverage report` → TOTAL 97% (≥ 94 floor;
  baseline 96%); `backends/dify.py` 98%.
- `uv run ruff check .` → `All checks passed!`; `uv run ruff format --check .` → clean.
- `uv run basedpyright` → `0 errors, 0 warnings, 0 notes`.
- `uv run pre-commit run --all-files` → all hooks passed.
- CI floor leg (throwaway detached worktree, HA 2026.7.0 overlay per `.github/workflows/lint.yml`):
  `uv run --no-sync pytest -q tests/` → `481 passed, 2 skipped` (the 2 skips are the
  pre-existing probatio-only tests).
- Mutation check: reverting the `agent` mode, the stop-on-exception, or the EOF raise each
  turns 2 Dify tests red.

Owner-run: the 12-row checklist in `scripts/e2e/README.md` (dify section), driven partly by
`scripts/e2e/dify_e2e.py`; results go in `docs/implementation/e2e-results/dify.md` (NOT RUN).

## Risks / open questions
- Older self-hosted Dify may lack `agent` mode / `reasoning_chunk` / `workflow_paused` — all
  handled as unknown-and-ignored or by the EOF rule; live rows are the real check.
- `dify_user_from_ha` with `device`/`agent` scope: a conversation created under one user is
  invisible to another (spec `user` scoping), so a different user on the same key starts a
  new Dify conversation via the 404 recreate path. Documented, not "fixed".
- `max_length` truncation of HA-context inputs relies on Dify returning `max_length` in
  `user_input_form` (present in Dify responses, not in the spec's schema) — **inferred**.
- Diagnostics do not redact `dify_inputs` (`diagnostics.py` `TO_REDACT`); static inputs could carry personal context — candidate follow-up to add the key.
