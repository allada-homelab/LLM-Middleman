# Session handoff — v1 released, owner-run remainder

> Re-entry point after a Claude Code window reload. The v1 re-architecture is **complete and
> released as `v1.0.0`**. This file now tracks only the small owner-run remainder plus the
> standing orchestration rules that must survive a reload.

## Current state (v1.0.0 released)

- **All implementation tickets merged** to `main`: LLMM-001…017 done; LLMM-018 (live E2E
  matrix) and LLMM-019 (release engineering) are effectively complete — see below.
- **Released:** tag `v1.0.0`, `manifest.json` version `1.0.0`, `CHANGELOG.md` `[1.0.0]`
  dated 2026-07-05. GitHub release cut.
- **Gate on `main`:** 221 tests passing, strict basedpyright 0 errors,
  `uv run pre-commit run --all-files` exit 0, hassfest green.
- **Live E2E dress rehearsal PASSED** for all five presets + HACS packaging against real
  backends — evidence in `docs/implementation/e2e-results/` (`MATRIX.md` first, per-preset
  `.md` + raw captures alongside). Three real defects the rehearsal caught were fixed and
  merged before release: the subentry-lifecycle reload listener, the openai_compat `/v1`
  base-URL trap, and the langgraph `event: end` dead code.
- All `llmm-e2e-*` containers/volumes were torn down and rehearsal credentials deleted.

## The only work left — owner-run

These cannot be observed from the devcontainer; they need David's hands/hardware. When done,
flip **LLMM-018** and **LLMM-019** to `done` (their adapter-side and packaging criteria are
already met and evidenced).

1. **Live-HA HACS install of `v1.0.0`** — in the real HA: HACS → three-dot menu → *Custom
   repositories* → add `https://github.com/allada-homelab/LLM-Middleman` (category:
   Integration) → *Download* → restart (~60 s GitHub Device Flow). Everything downstream of
   that click is already proven (identical published artifact loads clean — `e2e-results/hacs.md`).
2. **Tier-1 tool-call rows against a capable model** — the dress rehearsal proved the tool
   loop is correct but a 0.6–1.5B local model never emits a valid `HassTurnOn` slot. Provide
   `~/.llmm-e2e.env` (Tier 1 of `scripts/e2e/E2E-ENABLEMENT-GUIDE.md`), then re-drive the
   `openai_compat` + `ollama` tool rows against the owner's llama.cpp/proxy and watch
   `input_boolean.llmm_e2e_test` actually flip. Recipe: `scripts/e2e/README.md`.
3. **Voice-hardware checks** (Tier 2) — wake-word follow-up with mic kept open
   (`continue_conversation`) and the time-to-first-audio feel on a real satellite. Batch
   these with the HACS device-flow click above so it's one interactive session.

The E2E regression rig (recipe + stub + enablement guide) now lives in the repo under
`scripts/e2e/` so it survives container rebuilds and a future agent can re-run the matrix
after an HA version bump.

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
