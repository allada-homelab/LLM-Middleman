# AGENTS.md

Guidance for coding agents (Claude Code, pi, dsh) working in **LLM Middleman**.

This file is agent-facing. It is a starting point — ground-truth lives in code, not here.
Verify directory layout and recipe availability against `git log` and the actual
filesystem before trusting any specific claim below.

## What this is

**LLM Middleman is a HACS custom integration** (domain `llm_middleman`) that plugs into HA
Assist/Voice as a streaming text-only `ConversationEntity` and **forwards each recognized
turn to an external backend**, streaming the reply back into the pipeline (→ streaming TTS
and the Assist chat). It runs **no LLM of its own** — intelligence lives in the backend.

**Current state (2026-10): v1 shipped — maintenance mode.** The v1 multi-backend
re-architecture was released as `v1.0.0`; the latest release is **`v1.5.0`** (the
`manifest.json` `version` is the source of truth; `pyproject.toml` mirrors it, enforced by
`tests/test_versions.py`). Minimum Home Assistant is **2026.7.0** (`hacs.json`). What
shipped:

- **Six backend presets** behind a common adapter layer, dispatched by
  `backends/BACKEND_TO_CLS` (`get_backend_cls`): `converse` (the old v0 `/v1/converse`
  contract, now one preset among many), `openai_compat`, `ollama` (native), `langgraph`,
  `n8n`, and `dify` (added in v1.1.0). Each adapter subclasses `BackendAdapter`
  (`backends/base.py`).
- **Parent-entry + conversation-subentry config model** — a connection (backend type, URL,
  auth) is the parent entry; each conversation agent (name, prompt, options) is a
  `conversation` subentry.
- **HA tool loop** (`CONF_LLM_HASS_API`) for the tool-capable presets — `openai_compat` and
  `ollama` set `supports_ha_tools = True`; the others are `False`.
- **Per-agent memory scopes** for the stateful presets, **v0→v1 config-entry migration**
  (`async_migrate_entry`, entry version 2), and **redacted diagnostics** (`diagnostics.py`).

`v1.2.0` (2026-10-25) added reauth + a backend-unreachable repair issue, a setup-time
backend probe, token stats and reasoning passthrough, backend tool activity in the Assist
chat, and the **tier-1 reworks of Dify (LLMM-021) and LangGraph (LLMM-022)** — both
`in-review`, waiting only on owner-run live rows. `v1.3.0` (2026-10-25) added **AI Task
`generate_data` (LLMM-023)** — unit-gated, also `in-review` pending owner-run live rows —
plus info-level backend down/recovered logging and README reference tables. `v1.4.0`
(2026-10-25) fixed the Timeout regression (since 1.1.4 any backend silent for 30 s failed;
Timeout is now the maximum silence) and shipped the **tier-1 reworks of OpenAI-compatible +
Ollama (LLMM-024) and n8n + converse (LLMM-025)**, both `in-review` pending owner-run rows.
`v1.5.0` (2026-10-25) marks conversation and AI task entities unavailable while their
backend is unreachable (the fallback is still spoken), completing the quality scale.

Tickets LLMM-001…017 are done; LLMM-018 (live E2E matrix), LLMM-019 (release), LLMM-021 and
LLMM-022 are `in-review` pending owner rows, and so are LLMM-023 (AI Task `generate_data`
subentries for openai_compat/ollama), LLMM-024 and LLMM-025. The remaining work is
**owner-run** (Dify/LangGraph/AI Task/LLMM-024/LLMM-025 live rows, reauth on a real HA, HACS install of the next release, tool-call rows
against a capable model, voice-hardware checks) — see `docs/implementation/HANDOFF.md`.

**Quality scale:** `custom_components/llm_middleman/quality_scale.yaml` is the
per-rule self-assessment (core's format, rule list pinned to HA 2026.10.3). Every rule
is done or exempt, so the self-assessment is Platinum. Nothing validates the file — hassfest skips it for
custom integrations — so keep it honest by hand when a rule's status changes. The manifest
sets no `quality_scale` (see the file's header).

> **START HERE for background:** `docs/implementation/plan.md` — the architecture of record
> that v1 was built to — and `docs/implementation/README.md`, whose ticket index (with a
> **fast-follow roadmap** for post-v1 features) and workflow rules still govern new work.
>
> `docs/knowledge/` is background research (HA reference, decisions, glossary) — mostly
> sound but partly stale relative to the shipped code. `docs/archive/` holds docs kept only
> for reference — its `01-home-assistant-reference.md` is from the sibling
> LLM-Home-Controller repo and describes code that does NOT exist here.
> `docs/external-agent-handoff/` specs a *separate* external-agent service (its own repo);
> its "frozen" `/v1/converse` contract is just the `converse` preset here. **When any doc conflicts with the code, the code wins — fix the doc.**

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
- `docs/knowledge/` — background knowledge base; `docs/archive/` — reference-only docs
  that don't describe this repo; `docs/external-agent-handoff/` — spec bundle for the
  separate external-agent service (don't build that here).
- `.github/workflows/` — CI: `lint.yml` (lockfile check, ruff, basedpyright, pytest on an HA
  matrix — `locked` = latest HA in `uv.lock` with the coverage gate, `floor` = the
  `hacs.json` minimum — plus pre-commit and a full-history gitleaks scan), `validate.yml`
  (hassfest + HACS validation), and `release.yml` (on a `vX.Y.Z` tag: checks the tag
  against `manifest.json` and `CHANGELOG.md`, then creates the GitHub release).
- `pyproject.toml` — dev dependencies and tool config (ruff, basedpyright, pytest);
  `pyrightconfig.json` scopes the type-check to `custom_components/llm_middleman` + `tests`.
- `justfile` — the task runner; every routine command has a recipe.
- `.claude/` — project hooks and settings (committed, except `settings.local.json`).
- `.agents/skills/` — project skills and slash commands; `.claude/skills` is a symlink to it.
  Keep instructions in this file: a `CLAUDE.md` or `CLAUDE.local.md` makes Claude Code
  stop reading `AGENTS.md`.

## Workflow (post-v1 maintenance)

v1 is released, so most work now is bug fixes, dependency bumps, and the **fast-follow**
features listed at the bottom of `docs/implementation/README.md` (token stats,
tool-activity surfacing and reasoning passthrough shipped in v1.2.0; the AI Task subentry,
LLMM-023, in v1.3.0).

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

- `just sync` — sync deps to the lockfile (`uv sync --locked --all-groups --all-extras`);
  `just install` also installs the pre-commit hooks. The devcontainer's
  `.devcontainer/post-create.sh` does both on create.
- `just test` — run the unit suite.
- `just coverage` — the suite under coverage with the `fail_under` gate (`pyproject.toml`).
- `just lock-check` — verify `uv.lock` is current.
- `just lint` / `just lint-fix` — ruff check (with autofix).
- `just fmt` / `just fmt-check` — ruff format.
- `just typecheck` — basedpyright (strict).
- `just pre-commit` — run all pre-commit hooks across the repo.

Prefer the recipe over the raw command so behavior matches CI.

## Hard rules

These are non-negotiable. A project hook (`.claude/hooks/deny-guardrails.sh`)
deterministically blocks the worst of them, but the responsibility is yours.

1. **Never commit directly to `main`.** Branch, push, open a PR.
2. **Never force-push without explicit human confirmation**, and then only
   `--force-with-lease` — never bare `--force` / `-f` / `+refspec`. By default, rebase
   only unpublished work; update a published branch by merging `origin/main`.
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
