# Session handoff — maintenance mode, owner-run remainder

> Re-entry point after a Claude Code window reload. v1 is released (latest tag `v1.1.4`);
> `main` carries the unreleased post-1.1.4 work listed under `## [Unreleased]` in
> `CHANGELOG.md`. This file tracks only what still needs the owner's hands, plus the
> standing orchestration rules that must survive a reload.

## Current state

- **Released:** `v1.1.4` (`manifest.json` version is the source of truth). Minimum HA
  2026.7.0; CI runs the suite on the locked HA (2026.10.3, with the coverage gate) and on
  that floor.
- **Merged since v1.1.4, unreleased:** HACS validation / coverage gate / release workflow
  (#90), cross-preset contract + streaming gates (#91), adapter helpers (#96), robustness
  and HA idioms incl. the setup probe (#97), token stats + reasoning (#98), LangGraph
  tier-1 LLMM-022 (#99), reauth + repair issue (#100), Dify tier-1 LLMM-021 (#101), and
  the cleanup PR (external tool calls not replayed, per-agent device memory, Store removal
  via `store_keys`, newly exposed agent options).
- **Tickets in review, waiting on owner rows:** LLMM-021 (Dify), LLMM-022 (LangGraph),
  LLMM-023 (AI Task `generate_data`), LLMM-018 (live E2E matrix), LLMM-019 (release
  engineering).
- Live E2E evidence lives in `docs/implementation/e2e-results/` (`MATRIX.md` first).

## The only work left — owner-run

None of these can be observed from the devcontainer. When a group passes, record it in the
named results file and flip the ticket.

1. **Dify tier-1 rows** (`e2e-results/dify.md`, all 13 NOT RUN) against the owner's Dify
   and live HA — every app type, recovery, cancellation, setup errors, usage, inputs,
   satellite follow-up, tool activity, and the "New Jarvis" regression. Recipe and helper
   are in that file. Then LLMM-021 → `done`.
2. **LangGraph tier-1 owner rows** (`e2e-results/langgraph-tier1.md` § Owner-run rows): a
   real tool-calling model in a ReAct graph, a LangSmith cloud deployment, a self-hosted
   server with Bearer custom auth, voice hardware (interrupt follow-up, tool calls in the
   Assist chat), and a UI pass of the new config-flow fields. Then LLMM-022 → `done`.
3. **Reauth on a real HA** (#100): revoke a backend key → the entry asks to reauthenticate,
   only the credential field is shown, the entry reloads after the new key. Also stop a
   backend and watch the "backend unreachable" repair appear after three turns and clear
   on recovery.
4. **Device-scope upgrade check** (cleanup PR): on an install with a v1.1.x Dify or
   LangGraph device-scope agent, confirm the first agent to talk on that device keeps its
   history after upgrading, and a second agent on the same connection starts fresh.
5. **Live-HA HACS install of the next release** (tag after the unreleased work):
   HACS → *Custom repositories* → `https://github.com/allada-homelab/LLM-Middleman`
   (Integration) → *Download* → restart. The release workflow (#90) builds the GitHub
   release from the tag and the `CHANGELOG.md` section.
6. **Tool-call rows against a capable model** (carried over from v1.0.0): re-drive the
   `openai_compat` + `ollama` tool rows with `~/.llmm-e2e.env` (Tier 1 of
   `scripts/e2e/E2E-ENABLEMENT-GUIDE.md`) and watch `input_boolean.llmm_e2e_test` flip.
   Then LLMM-018 → `done`.
7. **AI Task rows** (`e2e-results/ai-task.md`, all 7 NOT RUN): `ai_task.generate_data`
   with and without a `structure`, plus an image attachment against a vision model, on a
   real OpenAI-compatible server and a real Ollama. Then LLMM-023 → `done`.
8. **Voice-hardware checks** (Tier 2, carried over): wake-word follow-up with the mic kept
   open and time-to-first-audio on a real satellite. Batch with items 2, 3 and 5 in one
   interactive session. Then LLMM-019 → `done` together with item 5.

The E2E regression rig lives in `scripts/e2e/` (re-run the matrix after an HA bump per its
`README.md`).

## Standing orchestration rules (must survive a reload)

- David's instruction: **Fable = orchestrator only; delegate scoped work to Opus subagents**
  (GSD spine at `~/.claude/plugins/cache/public-skills/get-shit-done/*/workflows/gsd.workflow.js`,
  `tier: "opus"`).
- Gate every merge on real exit codes — never through a pipe (`cmd > f; rc=$?`), and
  `gh pr checks` must show checks exist before watching (this bit twice).
- Workflow worktree isolation may not provision — briefs must include worktree
  self-provisioning; the canonical checkout stays on `main`.
- Repo hard rules apply (see `CLAUDE.md`): never commit to `main`, never force-push, never
  `--no-verify`, never merge red.
- Session memory: `~/.claude/projects/-workspaces-LLM-Middleman/memory/` holds the full rule
  set (`gsd-orchestration-llmm.md`) and project state (`llmm-v1-implementation-state.md`).
