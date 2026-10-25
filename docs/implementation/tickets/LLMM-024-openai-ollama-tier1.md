---
id: LLMM-024
title: OpenAI-compatible + Ollama tier-1 (mid-stream errors, truncation, max_completion_tokens, refusal, tool ids, think levels, cache key, quota)
status: in-review
phase: 5
depends_on: [LLMM-008, LLMM-010, LLMM-014, LLMM-015]
---

# LLMM-024 — OpenAI-compatible + Ollama tier-1

## Context
LLMM-021 (Dify) and LLMM-022 (LangGraph) set the tier-1 bar: spec-exact terminal states, no
silent failures, real-server captures replayed through the adapter. A 2026-10-25 audit of the
two stateless-replay presets against live Ollama 0.40.6 and llama.cpp server b11854 (plus the
documented vLLM / OpenRouter / OpenAI / Ollama error shapes) found real bugs and missing
capabilities. Architecture is unchanged (`plan.md` §adapters): both stay stateless-replay,
tool-capable presets.

Sources: Ollama `api/types.go` (`Message.tool_name`/`tool_call_id`, `ToolCall.id`,
`ToolCallFunction.index`, `think` bool or level), https://docs.ollama.com/api/errors (mid-stream
`{"error"}` NDJSON line, HTTP 200); OpenAI chat/completions reference (`max_tokens` not
supported by reasoning models, `max_completion_tokens`, `reasoning_effort`,
`prompt_cache_key`, `delta.refusal`, `usage.completion_tokens_details.reasoning_tokens`,
`finish_reason`); vLLM `serving_chat.py` (error frame then `[DONE]`); OpenRouter "Mid-stream
errors" (top-level `error` + `finish_reason: "error"`); core HA 2026.10.3 `ollama/const.py`
and `openai_conversation/entity.py`.

## Scope
**In** (`backends/openai_compat.py`, `backends/ollama.py`, `backends/base.py`
(`trace_token_stats`, `TurnContext.subentry_id`, truncation helper), `conversation.py` /
`ai_task.py` (pass the subentry id), the two presets' parts of `config_flow.py` / `const.py` /
`strings.json` / `translations/en.json`, `tests/backends/test_{openai_compat,ollama}.py`,
fixtures under `tests/backends/fixtures/{openai_compat,ollama}/`, README option tables,
CHANGELOG):
1. Mid-stream error frames raise `BackendStreamError`: openai_compat top-level `error` (vLLM,
   OpenRouter) and `finish_reason: "error"`; ollama `{"error": …}` NDJSON line.
2. Truncation: openai_compat `finish_reason` `length` / `content_filter` and ollama
   `done_reason` `length` keep the text, log one warning and trace `{"finish_reason": …}`.
3. `max_tokens` rejected (HTTP 400/422 naming `max_tokens` as unsupported) → retry once with
   `max_completion_tokens`, remembered per model on the connection.
4. `delta.refusal` streams as reply text.
5. Ollama tool calls keep the server's `id`; replay sends `id` + `function.index` and the tool
   result with `tool_name` + `tool_call_id`.
6. Ollama `think` accepts off / on / low / medium / high / max; stored booleans keep working.
7. Ollama `num_ctx` / `keep_alive`: **documented recommendation, no forced default.** An
   empty option is still not sent. The README recommends Context window ≥ 8192 when an HA
   LLM API is enabled (HA's tool prompt is large; Ollama's own default can be 4096 on smaller
   GPUs) and setting Keep alive if model loads are slow. Why not core HA's 8192 / -1:
   sending them overrides the user's server config. It would shrink the context on big-GPU
   hosts where newer Ollama sizes `num_ctx` by VRAM, and pin models in VRAM against
   `OLLAMA_KEEP_ALIVE`. The original reason (a model load tripping a 30 s idle timeout) went
   away when #110 made the agent Timeout the longest allowed silence.
8. openai_compat: `reasoning_effort` option (sent only when set); `prompt_cache_key` = the
   agent's subentry id (core openai's choice), dropped for the connection if a server rejects
   it; HTTP 429 → translated `backend_rate_limited` error naming rate limit / quota.
9. Trace extras: `reasoning_tokens` (openai `usage.completion_tokens_details`) and Ollama
   `prompt_eval_cached_count` as `cached_input_tokens`.
10. Ollama `/api/show` capabilities to hide tools/think — see Out.

**Out:** Responses API; model pulling; base-URL path migration (e.g. Gemini
`/v1beta/openai`); item 10 (`/api/show` capability gating) — the model is picked in the same
form that would hide the fields, so gating needs a two-step form or a re-render; a new agent
has no model yet. Speaking the 429 reason in a *conversation* (the entity guard speaks the
generic fallback for every adapter error; AI Task surfaces the translated message).

## Implementation notes
- Partial text on a mid-stream error matches Dify (`dify.py` `_run_stream`, `error` event):
  log at ERROR, raise `BackendStreamError`; the entity guard keeps the spoken partial text,
  appends the fallback on a new line and marks the turn failed. Accumulated tool calls are
  not flushed.
- Truncation keeps the text because a cut-off spoken answer is more useful than a fallback,
  and the guard cannot un-speak streamed text anyway. Dify has no truncation signal to match;
  core openai (Responses API) raises after the text already streamed, which in this entity
  would only append a fallback to an otherwise usable answer.
- Request retries (`openai_compat._open_stream`): one loop that, on a 400/422 naming a
  retryable field, drops `stream_options` / `prompt_cache_key` (connection-wide) or swaps
  `max_tokens` for `max_completion_tokens` (per model), then re-POSTs. Each rejection removes
  a field from the request, so the loop is bounded. `max_tokens` is swapped only when the
  body also says "support" (OpenAI's `unsupported_parameter`); vLLM's "max_tokens is too large"
  is not retried. The default stays `max_tokens`: Ollama's `/v1` shim ignores
  `max_completion_tokens` (captured).
- `prompt_cache_key` needs the subentry id, so `TurnContext` gains `subentry_id` (set by the
  conversation entity and the AI Task entity).
- 429 maps in the `_post_checked` `on_error_status` hook to `HomeAssistantError(
  translation_key="backend_rate_limited")` with the provider message as placeholder.
- Ollama think selector values: `off`, `on`, `low`, `medium`, `high`, `max` (Ollama 0.40.6's
  accepted set, captured `error_400_bad_think`); stored `True`/`False` map to `on`/`off`.
- Fixtures: JSON wrappers (`description`, `source`, `server`/`spec`, `request`, `status`,
  `body`) like `fixtures/langgraph/`; `source: spec-derived` marks shapes that can't be provoked
  on a local server (see `fixtures/README.md`).

## Acceptance criteria
- [x] 1 — error frames (vLLM, OpenRouter, Ollama) raise after partial text; captured replies unaffected.
- [x] 2 — length/content_filter keep the text, warn once, trace `finish_reason`.
- [x] 3 — 400 naming unsupported `max_tokens` retried once with `max_completion_tokens`, remembered per model; unrelated 400 not retried.
- [x] 4 — refusal-only stream speaks the refusal.
- [x] 5 — Ollama tool ids kept and replayed with index, `tool_name`, `tool_call_id`.
- [x] 6 — think levels sent; stored booleans still sent as booleans; selector offers the levels.
- [x] 7 — unset `num_ctx` / `keep_alive` not sent (`test_options_absent_when_unset`); README recommendation added.
- [x] 8 — `reasoning_effort` only when set; `prompt_cache_key` = subentry id, dropped once rejected; 429 → translated error.
- [x] 9 — `reasoning_tokens` and Ollama cached prompt tokens traced when reported.
- [x] Every captured fixture replays as a normal reply / tool call (no fallback).
- [ ] Owner-run live rows against OpenAI (reasoning model, quota) and vLLM/OpenRouter (NOT RUN).

## Verification
Agent-run (devcontainer, 2026-10-25, branch `feat/llmm-024-openai-ollama-tier1` off `d48e88a`):
- Baseline `uv run pytest -q` on `d48e88a` → `801 passed`.
- `uv lock --check` → rc 0.
- `uv run pytest -q` → `854 passed` (+53; the only existing tests changed are those whose
  contract this ticket changes on purpose: the Ollama tool replay shape, `think` submitted as a select value, `reasoning_effort` in the
  OpenAI-compatible agent and AI task forms).
- `uv run coverage run -m pytest -q && uv run coverage report` → TOTAL 98 % (≥ 94);
  `openai_compat.py` 99 %, `ollama.py` 97 % (uncovered lines pre-date this ticket).
- `uv run ruff check .`, `uv run ruff format --check .` → clean; `uv run basedpyright` →
  `0 errors, 0 warnings, 0 notes`; `uv run pre-commit run --all-files` → all hooks passed.
- CI floor leg (throwaway detached worktree, HA 2026.7.0 overlay per
  `.github/workflows/lint.yml`, removed after): `uv run --no-sync pytest -q tests/` →
  `848 passed, 6 skipped` (all six are HA-version gates that pre-date this ticket: probatio ×4,
  tool-result `error` flag ×2).
- Mutation checks (each reverted after): disabling the openai error-frame check turns 3
  tests red, `finish_reason: "error"` 1, refusal 1, the `max_tokens` swap 2, truncation
  recording 3; Ollama error line 1, `done_reason` length 1, keeping the tool id 1.
- Captures: Ollama 0.40.6 and llama.cpp b11854 (same qwen3 0.6b GGUF), see
  `tests/backends/fixtures/README.md`. They also confirmed: Ollama `/v1` ignores
  `max_completion_tokens` and accepts `prompt_cache_key` / `reasoning_effort`; llama.cpp
  honours `max_completion_tokens` and accepts `prompt_cache_key`; Ollama native returns
  `tool_calls[].id` + `function.index`, accepts replayed `tool_name` / `tool_call_id`, reports
  `prompt_eval_cached_count`, and accepts `think` `"low"` on qwen3.

Owner-run (NOT RUN): a reasoning model with Maximum tokens set and an out-of-quota key
against api.openai.com; a vLLM / OpenRouter mid-stream error; parallel tool calls on a
tool-capable Ollama model.

## Risks / open questions
- Stricter terminal handling could turn working replies into fallbacks — guarded by replaying
  every captured server shape (incl. Ollama `/v1` `content: ""` reasoning chunks and repeated
  usage) as a normal reply.
- An Ollama agent with HA tools on a small-GPU host may still run with a 4096 context
  (Ollama's own default) and truncate HA's tool prompt. This is mitigated by the README
  recommendation, not by code (item 7).
- `prompt_cache_key` sent to strict servers: the drop-on-rejection retry relies on the error
  body naming the field (Groq, Mistral, Gemini do per their docs — **inferred**, not live-tested).
