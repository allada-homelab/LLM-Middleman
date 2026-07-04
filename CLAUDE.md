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

**Current state (2026-07): mid-rewrite.** The code on `main` is **v0** — a single-backend
shim speaking one bespoke `/v1/converse` SSE contract. An approved **v1 re-architecture**
replaces it with **backend presets**: OpenAI-compatible, LangGraph, custom `/v1/converse`
(the v0 contract survives as one preset), Ollama native, and n8n — behind a common adapter
layer, a parent-entry + conversation-subentry config model, per-agent memory scopes, and an
optional HA tool loop (`CONF_LLM_HASS_API`) for tool-capable backends. v0 code is reference
material for the rewrite, not a base to extend.

> **START HERE:** `docs/implementation/plan.md` — the approved v1 architecture of record —
> and `docs/implementation/README.md` — ticket conventions, workflow rules, and the phased
> ticket index (LLMM-001…019) with dependency graph. Implementation work happens ticket by
> ticket; do not freelance outside a ticket's scope.
>
> `docs/knowledge/` is background research (HA reference, decisions, glossary) — mostly
> sound but partly stale relative to the v1 plan. **Caution:** `docs/knowledge/01-*.md` is
> retained verbatim from the sibling LLM-Home-Controller repo and describes code that does
> NOT exist here. `docs/external-agent-handoff/` specs a *separate* external-agent service
> (its own repo); the v1 plan demotes its "frozen" `/v1/converse` contract to one preset.

This is a **HACS custom integration, not a pip package** — there is intentionally no
`[build-system]`. Runtime deps are declared in `custom_components/llm_middleman/manifest.json`
(`requirements`); dev tooling in `pyproject.toml`.

## Where things live

- `custom_components/llm_middleman/` — the integration (currently v0; v1 adds a
  `backends/` adapter package per the plan).
- `tests/` — pytest + `pytest-homeassistant-custom-component` (MockChatLog pattern).
- `docs/implementation/` — **the project-management home for the v1 rewrite**: plan of
  record, ticket briefs (`tickets/LLMM-###-*.md`), statuses, dependency graph.
- `docs/knowledge/` — background knowledge base; `docs/external-agent-handoff/` — spec
  bundle for the separate external-agent service (don't build that here).
- `pyproject.toml` — dev dependencies and tool config (ruff, basedpyright, pytest).
- `justfile` — the task runner; every routine command has a recipe.
- `.claude/` — project hooks and settings (committed, except `settings.local.json`).

## Implementation workflow (v1 rewrite)

1. Work is ticketed: one ticket → one branch (`llmm-###-slug`) → one PR referencing the
   ticket ID. Ticket briefs are self-contained; read the ticket + `plan.md` before coding.
2. Update the ticket's `status` field in the same PR that changes its state; `done`
   requires all acceptance criteria checked and the ticket's Verification section actually
   executed, with evidence in the PR.
3. Scope discovered mid-ticket: small → amend the ticket in the same PR; large → file a
   new ticket. Never silently expand.

## Common tasks (`just`)

Run `just` (or `just --list`) to see everything. The core loop:

- `just install` — sync all dependency groups and install git hooks.
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
