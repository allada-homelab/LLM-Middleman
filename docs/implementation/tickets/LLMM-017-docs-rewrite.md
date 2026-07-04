---
id: LLMM-017
title: Docs rewrite (knowledge base, README, stale-claim fixes, CHANGELOG)
status: todo
phase: 4
depends_on: [LLMM-009, LLMM-010, LLMM-011, LLMM-012, LLMM-013]
---

# LLMM-017 — Docs rewrite (knowledge base, README, stale-claim fixes, CHANGELOG)

## Context

The v0 docs describe a single frozen `/v1/converse` contract and a text-only pure
passthrough shim. The v1 architecture makes `/v1/converse` **one preset among five** and
adds an optional HA tool loop, so the knowledge base, external-agent handoff bundle,
README, and CHANGELOG are stale in specific, enumerated ways. This implements
`plan.md` §Housekeeping (docs bullet) and the README note under §Adjacent HA AI
capabilities ("In v1").

Depends on Phase 2 being complete (all five adapters + migration exist) so the docs
describe shipped behavior, not intentions. Ground every doc claim against the code that
lands in Phases 1–3 — per `CLAUDE.md` §Ground-truth discipline, the code wins.

## Scope

**In:**
- `docs/knowledge/03-the-shim.md`: rewrite from "the shim forwards to THE `/v1/converse`
  contract" to "the shim is a multi-backend conversation agent; `/v1/converse` is the
  `converse` preset." Update the architecture, config-flow (parent + subentries), and
  the preset list.
- `docs/external-agent-handoff/` (`README.md`, `implementation-brief.md`,
  `llm-providers.md`): reframe `/v1/converse` as one optional preset the external agent
  MAY implement, not the boundary contract; de-conflict the "frozen contract" language.
- `docs/knowledge/01-home-assistant-reference.md`: **quarantine or trim** — it is
  verbatim sibling-repo (LLM-Home-Controller) content describing code that does not exist
  in this repo.
- Fix the specific stale claims listed below.
- `README.md`: describe the five presets + the parent/subentry config model; add the
  local-intents note.
- `CHANGELOG.md`: replace the stale "Initial project scaffold" with the v1 rewrite entry.

**Out:**
- Renaming the repo / resolving the "middleman = shim vs middleman = brain" naming
  collision — flagged below as an open question for the owner, **not** done here (plan
  §Housekeeping does not mandate a rename).
- Fast-follow feature docs (AI Task, trace stats, external tool-activity) — those ship
  with their tickets, not here.
- The manifest/hacs version-floor edits themselves — owned by **LLMM-001** (this ticket
  only makes prose match them).

## Implementation notes

**Specific stale claims to fix (from research-3.json, with file:line anchors — re-verify
each against the current file before editing, line numbers drift):**

1. **Doc 01 describes a different repo (biggest confusion risk).**
   `docs/knowledge/01-home-assistant-reference.md` (esp. `:191-241`, `:245-271`) is
   "retained verbatim" from LLM-Home-Controller and describes, in present tense, a
   provider Protocol + 3 adapters, `ai_task.py`, `sensor.py` usage sensors, memory tools,
   ~3.2k LOC, `entity.py:371-546`, `CONF_MEMORY_ENABLED`, etc. — none of which exist here.
   Quarantine (move under a clearly-labeled `reference/` or `_sibling-repo/` subfolder
   with a header stating it is external reference) **or** trim to only the HA-API facts
   that are true for this repo. Also fix `docs/knowledge/README.md:12`, which frames 01 as
   guiding "a rewrite of the sibling LLM-Home-Controller integration."

2. **`/v1/converse` presented as THE frozen contract.**
   `docs/knowledge/03-the-shim.md` §4, `docs/external-agent-handoff/implementation-brief.md`
   §4, and `docs/external-agent-handoff/README.md:37-40` ("contract FROZEN … consumer
   side already built") present `/v1/converse` as the boundary. Reframe: it is the
   `converse` preset (plan §Per-connector matrix — SSE `text_delta`/`done`/`error`); the
   external agent is now just one of five backend types HA can point at.

3. **`context` request field documented but never sent.**
   `03-the-shim.md:106` and `implementation-brief.md:119-122` document an optional
   `context: {area: ...}`. The shim never populates it. Mark it not-wired (or remove) so
   an agent author doesn't expect it.

4. **`continue_conversation` documented as consumed — now actually wired.**
   `03-the-shim.md:118`, `implementation-brief.md:137`, `:74-75` tell the agent to signal
   `continue_conversation` in `done`; v0 ignored it (only read `done.text` at
   `conversation.py:197`). Plan §Follow-up listening now wires it: the converse adapter
   ORs `done.continue_conversation` into the `ConversationResult`. Update the docs to
   describe the real v1 behavior (automatic `?`-detection for all presets + explicit
   override for converse/n8n) and resolve the contradiction with
   `06-glossary-and-references.md:149-150`.

5. **`done.text` phrasing implies it is authoritative.**
   `03-the-shim.md:129-130` ("Ensure the final chat_log content is … the `done.text`").
   In fact `done.text` is only used when no `text_delta` streamed; otherwise the
   concatenated deltas are final. Correct the phrasing.

6. **Resolved "open decisions" still listed as open.**
   `03-the-shim.md:174-182` lists transport, HA-side tool exposure, and
   single-agent-vs-subentries as open — all now decided by v1 (SSE/NDJSON per preset;
   HA tools optional via `CONF_LLM_HASS_API` on tool-capable presets; parent+subentries).
   Mark resolved.

7. **Backend matrix "open" vs "settled" contradiction.**
   `05-architecture-decisions-and-tradeoffs.md:159-162` and
   `implementation-brief.md:243` list the backend matrix as an open owner decision;
   `02-llm-backends-and-providers.md` §1 and `llm-providers.md` §1 present it as settled.
   It is now settled (plan §User decisions — five presets). Make them agree.

8. **CHANGELOG stale.** `CHANGELOG.md:7-11` says "Initial project scaffold" under
   `[Unreleased]`. Replace with the v1 rewrite summary (multi-backend presets,
   parent+subentry config, optional HA tool loop, diagnostics) under `[Unreleased]` or a
   dated version heading (coordinate the version string with LLMM-019).

**README local-intents note (plan §Adjacent — "In v1").** Add a short section: sentence
triggers and `prefer_local_intents` intercept turns **before** any conversation agent
runs, and HA's intent stage handles timers/simple device control ahead of the agent —
pre-empting "my agent never got the message" reports (research-2 constraint #3).

**README preset + config section.** Document the five presets (OpenAI-compatible,
LangGraph, custom `/v1/converse`, Ollama, n8n), the parent-entry (connection) +
conversation-subentry (per-agent) model, and which presets support the HA tool loop
(OpenAI-compatible, Ollama).

## Acceptance criteria

- [ ] `docs/knowledge/03-the-shim.md` describes the multi-backend architecture with
      `/v1/converse` as one preset; no remaining "the contract" framing.
- [ ] `docs/external-agent-handoff/` reframes `/v1/converse` as an optional preset and
      resolves the "frozen contract" language.
- [ ] `docs/knowledge/01-home-assistant-reference.md` is quarantined or trimmed so it no
      longer asserts non-existent code as present in this repo; `README.md:12`'s framing
      is fixed.
- [ ] All eight stale claims above are corrected (each traceable to an edit).
- [ ] `README.md` documents the five presets, parent/subentry model, tool-capable
      presets, and the local-intents note.
- [ ] `CHANGELOG.md` reflects the v1 rewrite, not "Initial project scaffold."
- [ ] Gates green: `just check` + `just typecheck` (docs-only changes must not break
      markdown/link checks in the gate).

## Verification

- `grep -rn "v1/converse" docs/ README.md` → every remaining hit frames it as a preset,
  not the boundary contract (manual read of each).
- `grep -rn "context\|continue_conversation\|done.text\|frozen" docs/` → each surviving
  hit matches the shipped v1 behavior (cross-read against `backends/converse.py` and
  `conversation.py` as landed in Phase 2/3).
- `grep -rn "provider Protocol\|ai_task.py\|CONF_MEMORY_ENABLED\|sensor.py"
  docs/knowledge/01*` → either zero hits or all under an explicit external-reference
  quarantine header.
- Read `README.md` end-to-end: five presets present, parent/subentry model present,
  local-intents note present.
- Read `CHANGELOG.md`: no "Initial project scaffold" line remains.
- Any repo doc-lint/link-check recipe in the gate passes (`just check`).

## Risks / open questions

- **Owner decision (open question, not resolved here):** whether to rename to kill the
  "middleman = the shim (this repo) vs middleman = the external brain" collision
  (`06-glossary-and-references.md:57-64` calls the brain "not this repo, despite the repo
  name"). Surface it to the owner; do not rename in this ticket.
- Line-number anchors above are from research-3.json against the v0 docs and will have
  drifted after Phases 1–3 doc-adjacent edits — locate each claim by content, not line.
- This ticket must land after the code it documents; if any Phase 2/3 behavior differs
  from plan (e.g. converse `continue_conversation` wiring changed), document the code as
  built and flag the plan divergence rather than documenting the plan.
