---
name: generate-python-project
description: Scaffold a new Python project from this Copier template. Interviews the user about library-vs-service and which optional features they need, recommends answers with rationale, runs copier to generate the project, and verifies the render. Use when the user wants to create, scaffold, or generate a new Python library or service from this template, or asks which project_type / include_* toggles they should pick.
---

# Generate a Python project from the template

Drive `copier` to create a new project for the user: interview → recommend → generate → verify.
Requires `copier` and `uv` (`uv tool install copier` if missing; or use `uvx copier`).

## 1. Determine the archetype (ask this first)

> "Is this an importable package others depend on (**library**), or a running app / FastAPI
> service (**service**)? A service is a library plus a FastAPI app shell, Dockerfile, and compose."

`project_type=service` is a superset of `library`.

## 2. Recommend answers (default to the schema default unless the user's need contradicts it)

Identity/toolchain — mostly mechanical; confirm the obvious, override `author_*`/`repo_owner` to the
real owner:

| Question | Default | Decide |
|---|---|---|
| `project_name` | *(required)* | Human name; everything derives from it. |
| `pkg_dist_name` | derived kebab-case | PyPI/dist name; accept default unless taken. |
| `description` | "A Python project." | One-liner. |
| `author_name` / `author_email` | repo owner's | Override to the real author. |
| `repo_owner` / `repo_name` | `allada-homelab` / dist name | GitHub `owner/repo`. |
| `license` | `MIT` | MIT (permissive) · Apache-2.0 (patent grant) · BSD-3-Clause · Proprietary. |
| `python_min` | `3.11` | Oldest Python to support (lint floor + type target). |
| `python_default` | `3.13` | Primary dev/CI interpreter. Must be `>= python_min` (validated). |
| `ci_runner_default` | `homelab-runners` | Pick `ubuntu-latest` if no self-hosted runner fleet. |

Feature toggles — **all default ON except `include_mcp_server`** (do not trust any "all on" prose).
Recommend OFF when the stated need matches the "off when" column:

| Toggle | Archetype | Default | Turn OFF when… |
|---|---|---|---|
| `include_devcontainer` | both | on | not using VS Code / Codespaces / containerized dev |
| `include_docs_site` | both | on | no MkDocs site wanted |
| `include_pr_title_check` | both | on | not enforcing Conventional-Commit PR titles |
| `include_integration_tests` | both | on | no testcontainers integration tier |
| `include_contract_tests` | both | on | no contract-test tier |
| `include_test_helpers` | both | on | don't want the shipped `testing/` helpers |
| `include_renovate` | both | on | not using Renovate (Dependabot still ships) |
| `include_extras_example` | both | on | don't want the `pretty`/`rich` optional-extra example |
| `include_pr_labeler` | both | on | no auto PR labeling |
| `include_discussions` | both | on | not using GitHub Discussions |
| `include_pypi_publish` | library | on | private / non-PyPI library |
| `include_docker_publish` | service | on | not publishing a GHCR image |
| `include_hadolint` | service | on | no Dockerfile lint |
| `include_mcp_server` | service | **off** | enable only if you want the FastMCP scaffold |

Only surface toggles the chosen archetype actually asks (the service-/library-only ones are
`when:`-gated).

## 3. Confirm

Show the resolved answers as a short YAML block and get a yes before generating.

## 4. Generate

Run against the **remote** template (latest tag, no `--vcs-ref`). Pass `--defaults` plus a `--data`
for every recommended answer:

```bash
copier copy --trust --defaults gh:allada-homelab/python-template <dest> \
  --data project_name="…" --data project_type=… --data license=… \
  --data include_<toggle>=false  # one per declined toggle
```

- **`--defaults` is required when running non-interactively** (i.e. when you, the agent, run it):
  without it copier tries to interactively prompt for any unsupplied question and fails with an
  `Errno 22` / selector error on a non-TTY stdin. `--defaults` takes the schema default for anything
  not given via `--data` — so supply a `--data` for each value you recommended that differs from the
  default. (A human running this in a real terminal may omit `--defaults` to be prompted.)
- **`--trust` is required**: post-generation `_tasks` run `uv sync`, `ruff format`, and (services)
  `cp -n .env.example .env`. Without it they're silently skipped and the render is incomplete.

## 5. Verify

In `<dest>`:

```bash
uv sync --all-extras --all-groups
uv run ruff check . && uv run ruff format --check . && uv run basedpyright \
  && uv run pytest -q && uv run coverage run -m pytest && uv run coverage report
```

Services also: `docker compose config >/dev/null`.

## 6. Report

Destination, archetype, toggles enabled/disabled (with reasons), gate result, and next steps
(`git init`, create `repo_owner/repo_name`, push). Mention `copier update` for future template
upgrades (the project ships an `update-project` skill for this).
