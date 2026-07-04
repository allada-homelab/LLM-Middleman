---
id: LLMM-018
title: Live E2E verification matrix (per-preset against real backends)
status: todo
phase: 4
depends_on: [LLMM-009, LLMM-010, LLMM-011, LLMM-012, LLMM-013]
---

# LLMM-018 — Live E2E verification matrix (per-preset against real backends)

## Context

Unit tests drive fake streams; this ticket proves each preset works end-to-end against a
**real** backend on the owner's live HA instance, per `plan.md` §Verification ("Live E2E
on the owner's HA instance"). It closes the gap the v0 build left (research-3: real-HA
voice smoke test, hassfest brands — all "NOT run") and is a release gate for LLMM-019.

Depends on Phase 2 complete (all five adapters + migration). Some checks depend on the
Phase 3 tool loop (LLMM-014/015) and are marked as such; run those only once Phase 3 has
merged. Voice-hardware checks are **owner-run** (flagged) — the implementing agent cannot
observe a mic/satellite.

## Scope

**In:**
- Install the integration on the owner's live HA via HACS custom repo.
- Run and record the per-preset live check matrix below (setup, observation, pass/fail).
- Produce a written results table pasted into the LLMM-019 release PR (and this ticket's
  PR) as the E2E evidence.
- Create + tear down the throwaway converse SSE stub and any sample workflows/graphs used.

**Out:**
- Fixing bugs found → file/return to the owning adapter ticket (LLMM-009–012) or a new
  ticket; this ticket only verifies and reports.
- Adding new automated unit tests (those live in the adapter tickets).
- CI changes (hassfest lives in `validate.yml`; the CI gate is LLMM-001/LLMM-019).

## Implementation notes

**Install path.** Add the repo as a HACS custom repository (category: Integration),
install, restart HA, add the integration via Settings → Devices & Services. Verify the
v0→v1 migration (LLMM-013) if a v0 entry is present: a v0 entry must upgrade to a
`converse` parent + one conversation subentry with no reconfiguration.

**Per-preset matrix** (observe per plan §Verification; the load-bearing observations are
streaming-TTS start, continuity across turns, and graceful fallback on backend-down —
research-2 constraints #2/#3):

| Preset | Real backend to point at | Key observations |
|---|---|---|
| OpenAI-compatible | Owner's local **llama.cpp / OpenAI-compatible proxy** | Streaming reply begins before completion (early TTS / delta stream); trailing-slash base URL still resolves; dummy/empty API key handling; multi-turn continuity in one session; **tool call** executes a HA device intent (needs LLMM-014) |
| LangGraph | A **`langgraph dev`** sample graph (MessagesState) | `messages-tuple` deltas stream; `assistant_id` default `"agent"`; node-filter suppresses tool-node chatter; thread continuity across turns (thread_id mapping); terminal `end`/`error` handled |
| Custom `/v1/converse` | A **minimal SSE stub** written in `/tmp` (emits `text_delta`/`done`, one case with `done.continue_conversation: true`, one `error` case) — **delete after** | `text_delta` streams to TTS; `done.continue_conversation` keeps the mic open (follow-up listening); `error` event → graceful fallback message, no hang |
| n8n | A **stock Chat Trigger → AI Agent** workflow | Streaming mode ON (both nodes stream-enabled) → NDJSON `StructuredChunk` streams; streaming toggle ON but workflow **not** stream-enabled → blocking body detected by content-type and still answered (no crash); `sessionId` continuity; missing output field surfaces an error, never speaks raw JSON |
| Ollama | Local **ollama** (`/api/chat`) | NDJSON `done:true` terminator; model dropdown from `/api/tags`; multi-turn continuity with `max_history` trim; **native tool_calls** execute a HA intent + malformed-args repair (needs LLMM-015) |

**Cross-cutting checks (every preset):**
- **Streaming-first-token / TTS start:** the reply begins rendering/speaking before the
  full text is ready (proves `_attr_supports_streaming` + delta streaming; ~0.5 s
  time-to-first-audio target with streaming TTS).
- **Continuity across turns:** a follow-up turn in the same session retains context
  (same `conversation_id` within the 5-min TTL; stateful presets via their session key,
  stateless via ChatLog replay).
- **Fallback on backend-down:** stop the backend mid-conversation → the entity speaks the
  fallback message and the pipeline never hangs (the never-hangs guard, LLMM-005).

**Converse SSE stub.** Write a tiny aiohttp (or `python -m http.server`-style) SSE server
in `/tmp` that speaks the plan §Per-connector converse contract. Include the
`continue_conversation` and `error` cases. Delete it when done (per `CLAUDE.md`
throwaway-scripts rule; state the one-line teardown: `rm` the `/tmp` file + stop the
process).

**Owner-run (voice hardware) — flagged.** Real voice-latency and wake-word/follow-up
checks on a physical satellite/mic are owner-run; the agent verifies the text pipeline
(Assist chat) and records the owner's voice observations when provided. Mark these rows
"owner-run" in the results table rather than claiming them.

## Acceptance criteria

- [ ] Integration installs cleanly via HACS custom repo on the live HA instance; v0→v1
      migration verified if applicable.
- [ ] Each of the five presets is exercised against its real backend with the matrix
      observations recorded (pass/fail + evidence).
- [ ] For every preset: streaming-TTS start, cross-turn continuity, and
      backend-down fallback are each observed and recorded.
- [ ] Converse SSE stub and any sample graphs/workflows are created for the test and
      **torn down** afterward (no `/tmp` or repo artifacts left).
- [ ] Tool-loop rows (OpenAI-compatible, Ollama) executed once LLMM-014/LLMM-015 are
      merged, or explicitly marked "deferred to post-Phase-3" with a reason.
- [ ] A results table is produced and linked into the LLMM-019 release PR.
- [ ] Gates green (unchanged): this ticket runs against the built artifact; `just check` +
      `just typecheck` remain green (no code changes expected here).

## Verification

The deliverable **is** the verification: a recorded, per-preset results table. For each
row capture concrete evidence — the exact utterance/probe, the observed streaming
behavior (first-token-before-completion yes/no), the follow-up-turn context result, and
the backend-down fallback text. For the converse stub and n8n, capture the request body
sent and the raw stream bytes observed (proves content-type branching and session
forwarding). Any row that cannot be observed (voice hardware) is labeled "owner-run",
not asserted.

## Risks / open questions

- **plan implementation-time checkpoint:** LangGraph `messages-tuple` frame shape and
  terminal event names are the researcher's least-confident item — this live capture
  against `langgraph dev` is where the LLMM-011 parser is finally confirmed. If the frames
  differ from what LLMM-011 assumed, that is a bug for LLMM-011, surfaced here.
- n8n's silent streaming degradation (workflow not stream-enabled on both nodes) is only
  reproducible live — the blocking-body-detection path (plan §Per-connector n8n) must be
  exercised against a real not-stream-enabled workflow, not just the fake.
- Backends the agent cannot self-host (owner's llama.cpp proxy, physical voice satellite)
  gate on owner availability; sequence the matrix so agent-runnable rows (converse stub,
  local ollama, `langgraph dev`) complete first and owner-run rows are batched.
