# Contributing to LLM Middleman

Thanks for contributing. This guide covers local setup, the quality gates, and the
release and ADR policies.

## Development setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management and
[just](https://just.systems/) as the task runner.

```bash
just install     # uv sync --all-groups --all-extras + install git hooks (pre-commit)
```

If you don't have `just`, the underlying commands are visible with `just --list`
or in the `justfile`.

### Dev container

Open the repo in the dev container (VS Code: *Reopen in Container*) for the full
toolchain without installing anything on the host. It works from a linked `git
worktree` as well as the main checkout — see [docs/devcontainer.md](docs/devcontainer.md)
for what makes that work and the rest of the container's contract.

From the terminal, drive it with the `@devcontainers/cli` (no editor needed):

```bash
npx -y @devcontainers/cli@0.87.0 up --workspace-folder .
npx -y @devcontainers/cli@0.87.0 exec --workspace-folder . -- just test
```

Pass the **host** path to `--workspace-folder`; never prefix these with
`DOCKER_HOST=…` — the container needs the rootful daemon, which is the default.

Git over SSH in the container goes through your **host's SSH agent**; no key
file is mounted. VS Code forwards the agent itself, and for the CLI
`devcontainer.json` mounts the socket named by `SSH_AUTH_SOCK` when the container
is created — see [docs/devcontainer.md](docs/devcontainer.md#ssh). So start the
agent (with your key loaded) **before** `up`. If none is running, use the script
for your platform. The macOS / Linux one must be *sourced*, so the agent's
environment reaches your shell:

```bash
. host_setup_scripts/start-host-ssh-agent-mac-linux.sh     # macOS / Linux
./host_setup_scripts/start-host-ssh-agent-windows.ps1      # Windows (PowerShell)
```


## Quality gates

Run these locally before pushing — CI runs the same checks:

```bash
just lint        # ruff check
just fmt-check   # ruff format --check
just typecheck   # basedpyright (strict)
just test        # pytest
```

Autofix and format in place:

```bash
just lint-fix    # ruff check --fix
just fmt         # ruff format
```

Run the full pre-commit suite (the same hooks CI enforces):

```bash
just pre-commit  # pre-commit run --all-files
```

## Tests

Write tests test-first when fixing a bug (a failing test that reproduces it, then
the fix). Keep new code fully type-annotated — `basedpyright` runs in strict mode
and untyped defs will fail the gate.

Integration tests live under `tests/integration/`, are marked
`@pytest.mark.integration`, and are **deselected from the default run**. They need
a Docker daemon — run them explicitly:

```bash
just test-int        # pytest tests/integration -m integration
```

Optional dependencies are exposed as extras (see `[project.optional-dependencies]`
in `pyproject.toml`). Install all of them for development with
`uv sync --all-extras --all-groups`.

## Pull requests

- Branch off the default branch (`feat/…`, `fix/…`, `chore/…`); never push to it
  directly.
- PR titles follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`type(scope): summary`) — a CI check enforces this.
- The full gate (lint, format, typecheck, test) must pass before merge.

## Release process

This is a HACS custom integration — there is no build backend and no PyPI
publish. Releases are SemVer and tag-based (HACS surfaces GitHub tags/releases
as installable versions). To cut a release:

1. Bump `"version"` in `custom_components/llm_middleman/manifest.json`.
2. Update `CHANGELOG.md`.
3. Land both on the default branch.
4. Tag the release commit with a matching `vX.Y.Z` tag and push it:

   ```bash
   git tag v1.2.3
   git push origin v1.2.3
   ```

Keep the tag in sync with the `manifest.json` version.

## Architecture Decision Records (ADRs)

Material decisions about the project, its workflow, or its architecture are
recorded as ADRs under [`docs/adr/`](docs/adr/). Before re-litigating a past
decision, read the relevant ADR first.

- Write a new ADR when a decision constrains future work, closes a question
  someone might re-open, or picks among plausible alternatives.
- ADRs are **append-only**: supersede with a new ADR rather than rewriting an
  accepted one.
- Copy [`docs/adr/template.md`](docs/adr/template.md) to start. See
  [`docs/adr/README.md`](docs/adr/README.md) for numbering and status conventions.
