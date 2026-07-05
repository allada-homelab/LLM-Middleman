# Session handoff — v1 rewrite orchestration

> Written 2026-07-05 by the orchestrating agent (Fable, orchestrator-only; all scoped
> work is delegated to **Opus subagents** — David's standing instruction). This file is
> the re-entry point after a Claude Code window reload. It is intentionally
> **uncommitted** until it rides along in the LLMM-018 results PR (hard rule: never
> commit to `main` directly).

## Current state (verified 2026-07-05)

- **17 of 19 tickets done and merged** to `main` (`cdecb35`), PRs #2–#20, every merge on
  observed-green CI. Phases 1–3 complete + LLMM-016/017.
- Gate on `main`: **218 tests passing, strict basedpyright 0 errors,
  `uv run pre-commit run --all-files` exit 0, hassfest green.**
- Every ticket had 2/2 independent Opus adversarial verification; three cross-ticket
  seams were caught and fixed at whole-tree integration (duplicate consts ×2, n8n
  flow↔adapter key mismatch, unused timeout helper → PR #14).
- Remaining tickets: **LLMM-018** (live E2E matrix — in progress, see below) and
  **LLMM-019** (release — preflighted, owner-gated).

## LLMM-018 Tier-0 dress rehearsal — COMPLETE (2026-07-05)

All five presets + HACS packaging PASS against real backends (results in
`docs/implementation/e2e-results/`, MATRIX.md first). Three real defects found and
fixed (PRs #21/#22/#23, merged green; main now `9a97aa2`+, 221 tests). All
`llmm-e2e-*` containers/volumes torn down, credentials deleted. Remaining for the
ticket to go `done`: owner-run rows only — live-HA HACS install (60 s device flow),
tool-call retest against a tool-capable model (needs `/home/vscode/.llmm-e2e.env`,
Tier 1 of the enablement guide), and the voice-hardware observations. Then LLMM-019
(version call, brands PR, GitHub release — all owner-gated; preflight done).

The section below is the historical plan of that rehearsal:

## In flight: LLMM-018 Tier-0 dress rehearsal (authorized by David 2026-07-05)

Plan (all implementer work = Opus subagents; Fable orchestrates):

1. **Wave E2E-A — DONE, confirmed 2/2 (2026-07-05):** disposable HA 2026.7.1 is live —
   container `llmm-e2e-ha`, reachable at `http://172.17.0.6:8123` (bridge IP; Bash-hook
   blocks curl to it — use python3 or `docker exec llmm-e2e-ha curl localhost:8123`),
   token + creds in `/home/vscode/llmm-e2e/credentials.env`, full notes in
   **`/home/vscode/llmm-e2e/STATE.md`**. Integration proven loadable via the flow API
   (backend_type form, 5 options); `input_boolean.llmm_e2e_test` exists.
2. **Wave E2E-B — RUNNING (workflow `wf_cac8ce4f-8ea`):** 5 parallel Opus row agents,
   each creating its own config entry in the shared throwaway HA:
   - converse — stub at `/home/vscode/llmm-e2e/converse_sse_stub.py` (pre-validated
     against the real adapter);
   - langgraph — local `langgraph dev` sample MessagesState graph (no LLM key needed);
     **highest-value row**: the messages-tuple frame shape is the plan's least-confident
     item; a mismatch is a bug for LLMM-011;
   - ollama — `docker run` ollama + small model (~1 GB pull, David approved Tier 0);
     includes the native tool-call row against a test `input_boolean` in the throwaway HA;
   - HACS rehearsal — separate HA container (`llmm-e2e-ha-hacs`), install HACS, add this
     public repo as a custom repository, install, prove the consumer path.
3. **Compile results** into `RESULTS-TEMPLATE.md` (kit in `/home/vscode/llmm-e2e/`),
   tear down all `llmm-e2e-*` containers/volumes, open the LLMM-018 PR (branch
   `llmm-018-live-e2e`, results table + ticket status update + this handoff file).

Owner-gated remainder (do NOT attempt): live-HA rows (needs `/home/vscode/.llmm-e2e.env`
from David — Tier 1 of `/home/vscode/llmm-e2e/E2E-ENABLEMENT-GUIDE.md`), voice-hardware
rows (owner-run), and all of LLMM-019's outward actions (version string, brands PR,
GitHub release — each needs David's explicit sign-off; preflight already done: codeowners
resolves, manifest/hacs consistent, brand art at
`custom_components/llm_middleman/brand/` is spec-compliant).

## Immediate task on window reload

1. `TaskList` → task #6 is the active one.
2. Check Wave E2E-A: `docker ps -a --filter name=llmm-e2e` and read
   `/home/vscode/llmm-e2e/STATE.md`.
   - STATE.md complete + container healthy → launch **Wave E2E-B** (parallel Opus
     subagents per row, via the GSD spine at
     `~/.claude/plugins/cache/public-skills/get-shit-done/*/workflows/gsd.workflow.js`).
   - STATE.md missing/incomplete → the A-wave died with the old session; relaunch it
     (single Opus subagent; brief is reproducible from this file + the enablement guide).
3. Check whether David created `/home/vscode/.llmm-e2e.env` (Tier 1) — if yes, add the
   live-HA + openai_compat + n8n rows to Wave E2E-B.

## Orchestration rules that must survive the reload

- David's instruction: **Fable = orchestrator only; delegate scoped work to Opus
  subagents** (GSD spine, `tier: "opus"`).
- Gate every merge on real exit codes — never through a pipe (`cmd > f; rc=$?`), and
  `gh pr checks` must show checks exist before watching (this bit twice).
- Workflow worktree isolation may not provision — briefs must include worktree
  self-provisioning; the canonical checkout stays on `main`.
- Session memory: `~/.claude/projects/-workspaces-LLM-Middleman/memory/` has the full
  rule set (`gsd-orchestration-llmm.md`) and project state
  (`llmm-v1-implementation-state.md`).

## Known minor debt (tracked, non-blocking)

- aiohttp `BasicAuth` deprecation warnings (2) in `n8n.py`.
- langgraph probe requests use default timeouts (fine for quick GETs).
- `_sse.py` BOM note recorded in LLMM-002's Risks (done in LLMM-017).
