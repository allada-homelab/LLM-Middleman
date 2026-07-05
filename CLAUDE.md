# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in **LLM Middleman**.

This file is Claude-facing. It is a starting point — ground-truth lives in code, not here.
Verify directory layout and recipe availability against `git log` and the actual
filesystem before trusting any specific claim below.

## What this is

**LLM Middleman is a HACS custom integration** (domain `llm_middleman`) that plugs into HA
Assist/Voice as a streaming text-only `ConversationEntity` and **forwards each recognized
turn to an external backend**, streaming the reply back into the pipeline (→ streaming TTS
and the Assist chat). It runs **no LLM of its own** — intelligence lives in the backend.

**Current state (2026-07): v1 shipped — maintenance mode.** The v1 multi-backend
re-architecture is **complete and released as `v1.0.0`** (tag `v1.0.0`, `manifest.json`
version `1.0.0`). The code on `main` is the v1 architecture, not the old v0 shim. What
shipped:

- **Five backend presets** behind a common adapter layer, dispatched by
  `backends/BACKEND_TO_CLS` (`get_backend_cls`): `converse` (the old v0 `/v1/converse`
  contract, now one preset among many), `openai_compat`, `ollama` (native), `langgraph`,
  and `n8n`. Each adapter subclasses `BackendAdapter` (`backends/base.py`).
- **Parent-entry + conversation-subentry config model** — a connection (backend type, URL,
  auth) is the parent entry; each conversation agent (name, prompt, options) is a
  `conversation` subentry.
- **HA tool loop** (`CONF_LLM_HASS_API`) for the tool-capable presets — `openai_compat` and
  `ollama` set `supports_ha_tools = True`; the others are `False`.
- **Per-agent memory scopes** for the stateful presets, **v0→v1 config-entry migration**
  (`async_migrate_entry`, entry version 2), and **redacted diagnostics** (`diagnostics.py`).

Every ticket (LLMM-001…017) is merged; LLMM-018 (live E2E matrix) and LLMM-019 (release)
are effectively done — the five-preset + HACS-packaging dress rehearsal passed against real
backends (`docs/implementation/e2e-results/`, MATRIX.md first) and the release is cut. The
only work left is **owner-run** (live-HA HACS install, tool-call rows against a capable
model, voice-hardware checks) — see `docs/implementation/HANDOFF.md`.

> **START HERE for background:** `docs/implementation/plan.md` — the architecture of record
> that v1 was built to — and `docs/implementation/README.md`, whose ticket index (with a
> **fast-follow roadmap** for post-v1 features) and workflow rules still govern new work.
>
> `docs/knowledge/` is background research (HA reference, decisions, glossary) — mostly
> sound but partly stale relative to the shipped code. **Caution:** `docs/knowledge/01-*.md`
> is retained verbatim from the sibling LLM-Home-Controller repo and describes code that
> does NOT exist here. `docs/external-agent-handoff/` specs a *separate* external-agent
> service (its own repo); its "frozen" `/v1/converse` contract is just the `converse` preset
> here. **When any doc conflicts with the code, the code wins — fix the doc.**

This is a **HACS custom integration, not a pip package** — there is intentionally no
`[build-system]`. Runtime deps are declared in `custom_components/llm_middleman/manifest.json`
(`requirements`); dev tooling in `pyproject.toml`.

## Where things live

- `custom_components/llm_middleman/` — the integration. Key modules: `conversation.py`
  (the backend-agnostic entity + never-hangs guard), `config_flow.py` (parent flow +
  conversation subentry flow), `__init__.py` (setup, update listener, `async_migrate_entry`),
  `diagnostics.py`, `const.py`, and `backends/` — the adapter package (`base.py`,
  `BACKEND_TO_CLS` in `__init__.py`, one module per preset, plus `_sse.py`/`_history.py`).
- `tests/` — pytest + `pytest-homeassistant-custom-component` (MockChatLog pattern);
  `tests/backends/` holds the per-adapter suites.
- `docs/implementation/` — the project-management home: `plan.md` (architecture of record),
  `README.md` (ticket index + fast-follow roadmap), ticket briefs (`tickets/LLMM-###-*.md`),
  `HANDOFF.md` (current owner-run remainder), and `e2e-results/` (the live E2E dress-rehearsal
  evidence — `MATRIX.md` first).
- `scripts/e2e/` — the throwaway-HA E2E regression rig: `README.md` (the repeatable recipe
  for re-running the preset matrix after an HA bump), `converse_sse_stub.py` (the converse
  backend stub), `E2E-ENABLEMENT-GUIDE.md` (what the owner must provide for owner-gated rows).
- `docs/knowledge/` — background knowledge base; `docs/external-agent-handoff/` — spec
  bundle for the separate external-agent service (don't build that here).
- `pyproject.toml` — dev dependencies and tool config (ruff, basedpyright, pytest);
  `pyrightconfig.json` scopes the type-check to `custom_components/llm_middleman` + `tests`.
- `justfile` — the task runner; every routine command has a recipe.
- `.claude/` — project hooks and settings (committed, except `settings.local.json`).

## Workflow (post-v1 maintenance)

v1 is released, so most work now is bug fixes, dependency bumps, and the **fast-follow**
features listed at the bottom of `docs/implementation/README.md` (AI Task subentry, token
stats, external tool-activity surfacing, more presets).

1. **Small fixes** (bug, dep bump, doc fix): branch (`fix/…` or a short slug) → PR → merge
   only on a green gate. No ticket required, but keep the change surgical.
2. **New features** stay ticketed: pick a fast-follow item (or file a new ticket following
   the conventions in `docs/implementation/README.md`), one ticket → one branch
   (`llmm-###-slug`) → one PR referencing the ticket ID. Read the ticket + `plan.md` first.
3. Update a ticket's `status` in the same PR that changes its state; `done` requires all
   acceptance criteria checked and its Verification section actually executed, with evidence
   in the PR.
4. Scope discovered mid-change: small → fold it into the same PR; large → file a new ticket.
   Never silently expand.
5. Re-running the live E2E matrix (e.g. after an HA version bump) is a maintenance task, not
   a rewrite — follow `scripts/e2e/README.md`.

## Common tasks (`just`)

Run `just` (or `just --list`) to see everything. The core loop:

- `just sync` — sync the dev dependency group (`uv sync --locked --dev`). The
  devcontainer's `.devcontainer/post-create.sh` runs `uv sync --all-groups` and installs
  the pre-commit hooks on create; run those two by hand outside the container.
- `just test` — run the unit suite.
- `just lint` / `just lint-fix` — ruff check (with autofix).
- `just fmt` / `just fmt-check` — ruff format.
- `just typecheck` — basedpyright (strict).
- `just pre-commit` — run all pre-commit hooks across the repo.

Prefer the recipe over the raw command so behavior matches CI.

## Hard rules

These are non-negotiable. A project hook (`.claude/hooks/deny-guardrails.sh`)
deterministically blocks the worst of them, but the responsibility is yours.

1. **Never commit directly to `main`.** Branch, push, open a PR.
2. **Never force-push** (`--force` / `-f` / `--force-with-lease`). Rebase onto
   `origin/main` and push normally; if a branch is published, add a new commit.
3. **Never bypass the gate** with `--no-verify`, and never amend-then-push a
   published commit.
4. **Never merge red.** Failing lint, types, or tests block the merge — full stop.
5. **Fix the gate, not the symptom.** When a check fails, fix the underlying
   cause. Don't silence the linter, loosen a type, skip a test, or lower the
   coverage floor to make red turn green.

## Anti-Potemkin rule

Every change must be **fully wired or explicitly flagged.** Do not leave a
function defined-but-uncalled, a flag parsed-but-ignored, a branch stubbed with
`pass`/`TODO`, or a test that asserts nothing, while implying the feature works.
If something is intentionally incomplete, say so plainly in the PR and in the
code (a `TODO` with context) — never present a stub as finished.

## Ground-truth discipline

Before acting on any assumption about this repo, confirm it against reality:

- `git log` / `git status` for what actually changed and the current branch.
- The filesystem for what files and recipes actually exist.
- `pyproject.toml` for versions and configured tools.
- Ticket statuses in `docs/implementation/tickets/` for what is actually done vs planned.

A claim in this file, a README, or a stale comment is a hypothesis until the
code confirms it. When they conflict, the code wins — and fix the doc.
