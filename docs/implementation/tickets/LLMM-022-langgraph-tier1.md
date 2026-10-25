---
id: LLMM-022
title: LangGraph tier-1 (speak only AI text, deterministic threads, cancel, interrupts, tool activity)
status: in-review
phase: 5
depends_on: [LLMM-011]
---

# LLMM-022 — LangGraph tier-1

## Context
LLMM-011 shipped a working LangGraph preset, but probing a live `langgraph-api` 0.16.6 server
(`docs/implementation/e2e-results/langgraph-tier1.md`) turned up gaps a production voice
assistant cannot have: tool output was spoken (the `messages` stream carries `type:"tool"`
frames), runs HA abandoned kept running server-side, graphs that call `interrupt()` dead-ended
in the fallback, and three agent options were read by the adapter but had no UI. This ticket
makes the preset the reference implementation: correct on the wire, observable in the Assist
chat, and robust to the Agent Server's double-texting and thread lifecycle. Architecture is
unchanged (`plan.md` §adapters): a stateful, text-only preset (`supports_ha_tools = False`).

## Scope
**In:** `backends/langgraph.py`; a preset-agnostic external tool-activity helper in
`backends/_compat.py` (Dify reuses it); LangGraph fields in `config_flow.py`, `strings.json`,
`translations/en.json`, `const.py`; a LangGraph section in `diagnostics.py`; tests replaying
real captures; a live E2E rig (`scripts/e2e/langgraph/`, `scripts/e2e/langgraph_live_e2e.py`);
README LangGraph section.

**Out:** HA tools for LangGraph (tools stay in the graph); thread TTL/metadata (`if_not_exists`
creates threads without a body for them); filtering external tool calls out of
openai_compat/ollama history replay (see Risks); deleting the pending-resume Store on entry
removal (see Risks).

## Implementation notes
- Stream with `stream_mode=["messages-tuple","updates"]`. Speak only `AIMessageChunk`/`ai`
  text from `messages`; take finished tool calls/results, `usage_metadata`, and
  `__interrupt__` from `updates` (shapes in the fixtures, `tests/backends/fixtures/langgraph/`).
- Threads: `uuid5(75e17f5a-ab70-5736-83d6-5c5f8ce93dca, "<entry_id>:<memory_key>")` with
  `if_not_exists:"create"`; the LLMM-011 Store map (`llm_middleman.langgraph.<entry>`) is read
  first, read-only, so upgraded device/agent threads keep their history.
- Run body: `on_disconnect:"cancel"`; threaded runs add `multitask_strategy` (default
  `rollback`). On `GeneratorExit`/`CancelledError`/`TimeoutError`/`aiohttp.ClientError` after
  the `metadata` frame, a background `POST /threads/{id}/runs/{run_id}/cancel` (5 s timeout;
  pattern from `dify.py` `_fire_stop`).
- Interrupts: speak `value` / `value.question` / `value.message`; set
  `ctx.continue_conversation`; mark the thread (in memory; device/agent scopes also in Store
  `llm_middleman.langgraph.<entry>.pending_resume`); next turn confirms via
  `GET /threads/{id}` that `interrupts` is non-empty (a cancelled run also leaves
  `status:"interrupted"`), then sends `{"command":{"resume": text}}`.
- `wants_time_prefix = False` (#97): the prompt (with HA's date/time) is in `context`, and a
  resume value must be the literal answer.
- A run with tool activity but no reply text raises `BackendStreamError` so the entity guard
  appends its fallback; the chat log never ends on a tool result (which would re-enter the HA
  tool loop and re-POST the turn).

## Acceptance criteria
- [x] 1. Only AI text is spoken: `tool`/`human` messages and tool-call-only chunks never reach TTS; whole `ai` messages from non-streaming nodes are spoken.
- [x] 2. `response_node_filter`, `stateless_runs`, `input_messages_key` are on the LangGraph agent form (strings + en.json), stored, and visible (non-secret) in diagnostics' subentry data.
- [x] 3. `on_disconnect:"cancel"` on every run; best-effort cancel of the run on HA cancel/timeout; `multitask_strategy` agent option, default `rollback` (justified by the live double-text probe: clean history, immediate answer).
- [x] 4. Interrupts are spoken, keep the satellite listening, and the next turn resumes with `command.resume` (confirmed against the thread first); the mark survives restart for device/agent scopes.
- [x] 5. Deterministic uuid5 threads with `if_not_exists:"create"`; no POST /threads, lock, or 404 retry; legacy Store map honoured read-only.
- [x] 6. Run `context` carries `system_prompt` (threaded), `language`, `device_id`, `satellite_id`, `conversation_id`, `user_id` (empty omitted) for threaded and stateless runs; contract documented in README.
- [x] 7. Connection option `auth_scheme` (`x_api_key` default for old entries / `bearer`).
- [x] 8. Setup checks the assistant exists (UUID → `GET /assistants/{id}`, graph id → `POST /assistants/search`); unknown → field error `assistant_not_found`; transport errors wrapped.
- [x] 9. Slow silent steps: verified live that the server heartbeats every 5 s, so the 30 s idle timeout is kept and documented (no new option).
- [x] 10. `_compat.external_tool_call_delta` / `external_tool_result_delta` (HA 2026.7 `tool_result` and ≥2026.10 `result` APIs); exactly one POST per turn with tool activity (entity test + live row).
- [x] 11. Token usage traced as `{"stats": {...}}` via `trace_token_stats`.
- [x] 12. Diagnostics include the last run id and counters; no secrets or thread ids.
- [ ] Owner-run rows in `e2e-results/langgraph-tier1.md` (real tool-calling model, LangSmith cloud, Bearer custom auth, voice hardware, HA UI pass).

## Verification
Executed 2026-10-24 on the branch head after merging origin/main (#97, #98):

```
uv run pytest -q                                   → 593 passed (baseline before the ticket: 443)
uv run coverage run -m pytest -q && uv run coverage report → TOTAL 96% (fail_under 94); langgraph.py 96%
uv run ruff check . / uv run ruff format --check . → rc=0 / rc=0
uv run basedpyright                                → 0 errors, 0 warnings
uv run pre-commit run --all-files                  → rc=0
CI floor leg (HA 2026.7.0, throwaway worktree, overlay per lint.yml):
  uv run --no-sync pytest -q tests/                → 589 passed, 4 skipped
Live (langgraph-api 0.16.6, scripts/e2e/langgraph on :2025):
  LANGGRAPH_E2E_URL=http://127.0.0.1:2025 uv run python -m pytest scripts/e2e/langgraph_live_e2e.py -v -s
                                                   → 9 passed (rows + observed values in e2e-results/langgraph-tier1.md)
```

## Risks / open questions
- **openai_compat/ollama replay external tool calls.** Their message conversion
  (`openai_compat.py` / `ollama.py` history builders) replays every `AssistantContent.tool_calls`
  without checking `external`. A chat log only contains LangGraph's external calls if the user
  switches agents mid-conversation (same conversation id); the replay is then a well-formed
  call + result pair the model never had. Not fixed here (other presets' files); follow-up.
- **Pending-resume Store not removed with the entry.** `async_remove_entry` (#97) deletes the
  legacy map Store only; `…pending_resume` (written only after a device/agent-scope interrupt)
  is left behind. Follow-up: add the key to `async_remove_entry`.
- **Two agents on one connection share device-scope threads** (same as LLMM-011: the session
  key is the device id, not per agent). Unchanged here; worth a ticket if personas diverge.
- `rollback` lets a second satellite on an `agent`-scope thread cut off the first speaker's
  in-flight reply; `enqueue` is the option for shared threads.
