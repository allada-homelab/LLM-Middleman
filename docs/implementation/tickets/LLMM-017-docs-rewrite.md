---
id: LLMM-017
title: Docs rewrite (knowledge base, README, stale-claim fixes, CHANGELOG)
status: in-review
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

**In (amended — small, sanctioned doc/config fixes bundled into this docs PR):**
- **Make `uv run pre-commit run --all-files` green.** Baseline on `main` was RED (the two
  items below); the docs gate now covers the whole-repo pre-commit run.
  - `LICENSE` ended with a stray extra blank line (`SOFTWARE.\n\n`), failing
    `end-of-file-fixer`. Fixed to a single trailing newline (`SOFTWARE.\n`). *(The parent
    brief's "add a trailing newline" was a mis-diagnosis of the byte state — the file
    already had a newline plus an extra blank line; the correct fix removes the extra one.)*
  - `codespell` flagged the hyphenated spelling of the "preempt" verb (and its -s / -ing
    forms) in three files. Reworded to the closed spelling "preempt" / "preempts" /
    "preempting" (no ignore flags added, per the fix-the-gate rule):
    `docs/implementation/plan.md` (§Adjacent, "preempts"), this ticket (README-note line,
    "preempting"), and `docs/implementation/tickets/LLMM-019-release-engineering.md`
    (hassfest section, "preempt").
- **`docs/implementation/tickets/LLMM-002-sse-reader.md`** — added a Risks note: `_sse.py`
  does not strip a leading UTF-8 BOM (WHATWG says discard one); low practical risk, tracked.
- **`CLAUDE.md`** — fixed a reference to a nonexistent `just install` recipe (the real
  recipe is `just sync`; the devcontainer's `.devcontainer/post-create.sh` runs
  `uv sync --all-groups` and installs the pre-commit hooks).

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
preempting "my agent never got the message" reports (research-2 constraint #3).

**README preset + config section.** Document the five presets (OpenAI-compatible,
LangGraph, custom `/v1/converse`, Ollama, n8n), the parent-entry (connection) +
conversation-subentry (per-agent) model, and which presets support the HA tool loop
(OpenAI-compatible, Ollama).

## Acceptance criteria

- [x] `docs/knowledge/03-the-shim.md` describes the multi-backend architecture with
      `/v1/converse` as one preset; no remaining "the contract" framing. *(Rewritten:
      §4 preset matrix, "one of five presets", `converse` as the reference preset.)*
- [x] `docs/external-agent-handoff/` reframes `/v1/converse` as an optional preset and
      resolves the "frozen contract" language. *(README "one hard rule" + brief §4 header
      reframed; scope note added; `context` field marked not-sent.)*
- [x] `docs/knowledge/01-home-assistant-reference.md` is quarantined (strong
      external-reference banner at the top stating it describes the sibling repo, not this
      one); `docs/knowledge/README.md`'s framing of 01 is fixed.
- [x] All eight stale claims above are corrected (each traceable to an edit — see
      Verification).
- [x] `README.md` documents the five presets, parent/subentry model, tool-capable presets
      (documented as built — see the tool-capability divergence note in Risks), and the
      local-intents note.
- [x] `CHANGELOG.md` reflects the v1 rewrite, not "Initial project scaffold."
- [x] Gates green: `just check` + `just typecheck`, plus `uv run pre-commit run
      --all-files` (see Verification for quoted output).

## Verification

Executed in the LLMM-017 worktree (branch `llmm-017-docs-rewrite`, off `main` @ 9cdb9e3):

- **Stale claim → edit traceability (all 8):**
  1. Doc 01 sibling-repo content — quarantine banner added to
     `docs/knowledge/01-home-assistant-reference.md`; `docs/knowledge/README.md` 01 row +
     summary reframed.
     `grep -rniE "provider Protocol|ai_task.py|CONF_MEMORY_ENABLED|sensor\.py"
     docs/knowledge/01-home-assistant-reference.md` → all hits are inside the quarantined
     verbatim body, under the explicit banner (banner names each term as sibling-repo
     reference). ✅
  2. `/v1/converse` as THE frozen contract — `03` §4, brief §4 header, and handoff README
     "one hard rule" all reframed to "one preset". `grep -rniE "frozen" docs/knowledge
     docs/external-agent-handoff` → no remaining contract-frozen framing (only the
     LLMM-002 `@dataclass(frozen=True)` reference remains, unrelated). ✅
  3. `context` request field — never sent (`backends/converse.py::stream_turn` body =
     `conversation_id`/`text`/`language`/`device_id?`). Marked not-sent in `03` §4 and
     brief §4. ✅
  4. `continue_conversation` now wired — documented in `03` §6, `06` glossary, brief §2.1
     + §4 handling (matches `conversation.py:162` OR-in and `converse.py:154-155`
     `done.continue_conversation` / `n8n.py:264-265` `continueConversation`). ✅
  5. `done.text` authoritative phrasing — corrected to "deltas authoritative; `done.text`
     only when nothing streamed" in `03` §4 and brief §4 (matches `converse.py:156-160`). ✅
  6. Resolved "open decisions" — `03` §10 marks transport / HA-tool exposure /
     single-vs-subentries as resolved by v1. ✅
  7. Backend-matrix open-vs-settled — `05` open-decisions §3 and brief §10 item 5 reframed
     to "target set settled; per-model tool-calling remains to verify", agreeing with
     `02` §1 / `llm-providers.md` §1. ✅
  8. CHANGELOG — `CHANGELOG.md` now carries the v1 rewrite entry; no "Initial project
     scaffold" line (`grep -n "Initial project scaffold" CHANGELOG.md` → no match). ✅
- `README.md` read end-to-end: five-preset table, parent/subentry config model, tool-loop
  section, follow-up listening, and the local-intents note all present.
- **Gates (all green, quoted in the PR):** `just lint`, `just fmt-check`, `just typecheck`
  (basedpyright strict, 0 errors), `just test` (212 passed), `just lock-check`, and
  `uv run pre-commit run --all-files` (exit 0 — codespell + end-of-file-fixer now pass).

## Risks / open questions

- **Owner decision (open question, not resolved here):** whether to rename to kill the
  "middleman = this HA integration vs middleman = the external brain" collision
  (`06-glossary-and-references.md` calls the brain "not this repo, despite the repo name").
  Surface it to the owner; do not rename in this ticket. Flagged in `03` §10.
- **Tool-capability documented as built, not as planned.** `plan.md` lists
  OpenAI-compatible **and** Ollama as HA-tool-capable, but on `main` only
  `openai_compat.py` sets `supports_ha_tools = True`; `ollama.py:121` is still `False`
  (its HA-tool wiring lands in LLMM-015, not yet merged). Per §Ground-truth discipline the
  docs describe the shipped code: README/`03` present **OpenAI-compatible as the
  tool-capable preset** and mark Ollama tool support as "not yet". When LLMM-015 merges,
  flip the two "not yet" notes (README preset table + `03` §4 footnote) to "yes".
- Line-number anchors in the claim list are from v0-era research and have drifted; each
  claim was located by content, not line.
- This ticket lands after the code it documents; where behavior differed from plan (the
  tool-capability note above), the code-as-built is documented and the divergence flagged.
