# python-template

A [Copier](https://copier.readthedocs.io/) template for new Python projects — a robust always-on
base plus optional, opt-out features, for two archetypes:

- **library** — a packaged Python library (`src/` layout, hatchling + dynamic version, PyPI Trusted
  Publishing, mkdocs site).
- **service** — everything in library **plus** a FastAPI app shell, Dockerfile, and compose.

## Usage

```bash
uvx copier copy gh:allada-homelab/python-template my-project
# or, with copier installed:  copier copy gh:allada-homelab/python-template my-project
cd my-project && just install
```

You'll be prompted for project identity, the `project_type` (library/service), Python versions, and a
set of `include_*` feature toggles (all default **on** — decline what you don't want; copier simply
won't render those files). Re-render against template updates with `copier update`.

## What you get

**Base (always):** uv + PEP 735 dependency groups, ruff (broad ruleset) lint+format, basedpyright
(strict), strict pytest + coverage gate, pre-commit (local ruff via uv), `just` task runner, editor +
git hygiene, base CI (lint/type/test matrix, `uv audit`, pre-commit), hardened Dependabot, a Dev
Container, `CLAUDE.md` + curated `.claude/` tooling with guardrail hooks, an ADR scaffold, and a
Keep-a-Changelog `CHANGELOG`.

**Optional toggles:** docs site (mkdocs-material + Pages), PyPI publish (library) / GHCR publish
(service), hadolint, conventional-commit PR-title check, integration tests (testcontainers), contract
tests, shipped test helpers, Renovate, MCP server (service).

## Quality gate

Every rendered project passes `just install && just lint && just typecheck && just test` (ruff,
basedpyright, pytest, coverage ≥ 80%). The template's own `render-test` workflow renders the
`library`/`service` × all-on/all-off cells and runs that gate on each, plus a no-dead-variable check.

## CI runners

Generated workflows default to `${{ vars.CI_RUNNER || 'homelab-runners' }}`. Set the `CI_RUNNER`
repository/organization variable to `ubuntu-latest` to use GitHub-hosted runners.

A **public** repo must set `CI_RUNNER=ubuntu-latest`: GitHub blocks self-hosted runners on public
repositories, so inheriting the `homelab-runners` default would leave jobs stuck with no runner.

## Repo layout

- `copier.yml` — the template schema (questions, toggles, gating).
- `template/` — the rendered project tree (`_subdirectory`, `.jinja`-suffix rendering).
- `docs/plans/` — design and dev-environment planning docs.
- `repos/` — vendored prior-art reference copies (not part of any render).
