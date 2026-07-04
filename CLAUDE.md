# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in **LLM Middleman**.

This file is Claude-facing. It is a starting point — ground-truth lives in code, not here.
Verify directory layout and recipe availability against `git log` and the actual
filesystem before trusting any specific claim below.

## What this is

**LLM Middleman is the Home Assistant "shim" integration** — a thin, text-only conversation agent
(a HACS custom integration, domain `llm_middleman`) that plugs into HA Assist/Voice and **forwards
each recognized turn to an external LLM agent** over the `/v1/converse` SSE contract, streaming the
reply back into the pipeline (→ TTS and the built-in Assist chat). It runs **no LLM and owns no
tools** — all intelligence lives in the external agent. This repo is **built and verified**.

> **START HERE:** `docs/knowledge/03-the-shim.md` — the shim's design (what it is, the HA plumbing,
> the shim⇄external-agent contract). `docs/knowledge/` is the full knowledge base (HA reference,
> decisions, glossary). The integration lives in `custom_components/llm_middleman/`.
>
> **NOT this repo:** `docs/plans/middleman-implementation-brief.md` and `docs/external-agent-handoff/`
> spec the *external* agent (the "brain") the shim forwards to — a **separate** service in its own
> repo. Don't build that here.

This is a **HACS custom integration, not a pip package** — there is intentionally no `[build-system]`.
Runtime deps are declared in `custom_components/llm_middleman/manifest.json` (`requirements`); dev
tooling in `pyproject.toml`.

## Where things live

- `custom_components/llm_middleman/` — the integration. `conversation.py` = the forwarding entity;
  plus `config_flow.py`, `__init__.py`, `const.py`, `manifest.json`, `strings.json`, `translations/`, `brand/`.
- `tests/` — pytest + `pytest-homeassistant-custom-component` (MockChatLog pattern).
- `docs/knowledge/` — the shim + system knowledge base; `docs/external-agent-handoff/` — the
  self-contained bundle for building the *external* agent (destined for its own repo).
- `pyproject.toml` — dev dependencies and tool config (ruff, basedpyright, pytest).
- `justfile` — the task runner; every routine command has a recipe.
- `.claude/` — project hooks and settings (committed, except `settings.local.json`).

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

A claim in this file, a README, or a stale comment is a hypothesis until the
code confirms it. When they conflict, the code wins — and fix the doc.
