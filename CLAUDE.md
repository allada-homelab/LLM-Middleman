# CLAUDE.md

Operating manual for working **on** this repo. This is a **Copier template**, not an
application — `copier.yml` is the schema and `template/` is the tree that gets rendered into a
consumer project. Read this before editing anything.

## What this repo is

`copier copy gh:allada-homelab/python-template <dest>` scaffolds a new Python project with a
robust always-on base plus full-by-default, opt-out features, in two archetypes:

- **`project_type=library`** — a packaged library (`src/` layout, hatchling + dynamic version,
  PyPI Trusted Publishing, mkdocs).
- **`project_type=service`** — everything in library **plus** a FastAPI app-factory, Dockerfile,
  and compose.

## Layout

| Path | What it is |
|---|---|
| `copier.yml` | The schema: questions, toggles, gating (`_exclude`, `_tasks`, `_skip_if_exists`), validators. **Frozen variable contract** — don't rename vars casually. |
| `template/` | The rendered tree. `*.jinja` files are rendered (suffix stripped); everything else is copied **verbatim**. |
| `.github/workflows/render-test.yml` | The template's own CI: renders the matrix cells + runs the gate in each. Defaults to `ubuntu-latest`. |
| `.github/render-test/answers/*.yml` | Checked-in answer files for the matrix cells. |
| `.github/render-test/no-dead-vars.sh` | Asserts every `copier.yml` question is consumed. |
| `.github/render-test/test_update.py` | PEP 723 pytest script: renders the latest tag → HEAD, asserts `copier update` merges cleanly. Run via `just update-test`. |
| `justfile` | Repo-root dev loop (`check`/`matrix`/`render-dirty`/`boot`/`update-test`/`clean`); renders into gitignored `.render/`. Not under `template/`, so it never renders. |
| `.pre-commit-config.yaml` | The repo's **own** hooks (shellcheck, check-jsonschema, codespell, no-dead-vars), scoped to exclude `template/`. Distinct from the one shipped in `template/`. |
| `.devcontainer/` | Dev container for developing the template (uv + Docker + just/pre-commit). Distinct from the one shipped in `template/.devcontainer/`. |
| `repos/` | Vendored prior-art reference (`arr-py-client`, `agents-scaffold`). **Read-only**; excluded from renders. Crib from it, never edit it. |

## The render + verify loop (do this for every template change)

A root `justfile` wraps this loop — `just check <cell>` (render + gate), `just matrix` (all cells
+ no-dead-vars, = CI), `just render-dirty <cell>` (include uncommitted changes), `just boot <cell>`
(service healthz probe), `just clean`. Renders land in gitignored `.render/<cell>/`. The raw loop
below is what those recipes run.

Copier renders from a **git ref**, so **commit first**, then render from `HEAD` and run the gate
inside the output:

```bash
git add -A && git commit -m "wip"
copier copy --trust --vcs-ref HEAD --defaults \
  --data-file .github/render-test/answers/lib-on.yml . /tmp/out
cd /tmp/out
uv sync --all-extras --all-groups
uv run ruff check . && uv run ruff format --check . \
  && uv run basedpyright && uv run pytest -q \
  && uv run coverage run -m pytest && uv run coverage report   # fail_under=80
```

Service cells additionally: `docker compose config` (or `just up` → probe `/healthz` → `just down`).

**Matrix** (run all before pushing): cells in `.github/render-test/answers/` —
`lib-on`, `lib-off`, `svc-on`, `svc-off`, `lib-variations`, `svc-variations` — each must pass the
gate; plus
`bash .github/render-test/no-dead-vars.sh` and `just update-test` (the automated `copier update`
merge check). CI runs exactly this on every PR/push, plus a `pre-commit` job for the repo's own hooks.

`--trust` is required because `_tasks` run (`uv sync`, `ruff format`, and for services
`cp -n .env.example .env`); without it the tasks are silently skipped and the render is incomplete.

## Copier gotchas (every one of these has bitten this repo)

- **Copier renders the latest git _tag_ by default, not `HEAD`.** Locally always pass
  `--vcs-ref HEAD`. A stale tag silently renders an old template (this repo inherited a `1.1.3`
  tag from its lineage that hijacked renders until deleted).
- **Leading-underscore `copier.yml` keys are settings, not questions.** A `_foo:` question is
  swallowed and `{{ _foo }}` renders empty. Use a normal name + `when: false` for computed/hidden
  vars — see `package_name` (`{{ pkg_dist_name | replace('-', '_') }}`).
- **`_exclude` patterns match the post-suffix-strip _destination_ path**, evaluated as Jinja —
  `mkdocs.yml`, never `mkdocs.yml.jinja`. A `.jinja` form is a silent no-op (the file renders
  regardless of the flag).
- **Wrap every GitHub Actions `${{ … }}` in `{% raw %}…{% endraw %}`** inside `.jinja` workflows.
  To inject a copier var into a GH expression, use the escape:
  `${{ '{{' }} vars.CI_RUNNER || '{{ ci_runner_default }}' {{ '}}' }}`. Every workflow's `runs-on`
  reads the single `vars.CI_RUNNER` variable (falling back to `ci_runner_default`), so a consumer
  overrides the runner for **all** jobs at once — no file edits — by setting that one variable:
  `gh variable set CI_RUNNER --body homelab-runners --org allada-homelab --visibility all` (org-level
  variables need the `admin:org` scope first: `gh auth refresh -h github.com -s admin:org`). Swap
  the `--body` for any runner label/class and the `--org` for any org (or drop `--org … --visibility`
  for a single-repo override); GitHub has no in-repo file a `runs-on` can read, so this variable is
  the only "declare once, reuse everywhere" override.
- **On macOS, `copier update` chokes on `/tmp` and `/var/folders` symlinks.** Test updates from a
  fully-resolved physical path: `OLD=$(cd "$(mktemp -d)" && pwd -P)`.
- **Don't let `template/.gitignore` shadow `.jinja` sources.** `.env.*` once ignored
  `.env.example.jinja` so it was never committed and silently dropped from renders; the fix was a
  `!.env.example.jinja` negation.
- **Just-native `{{ }}`** in `justfile.jinja` would collide with Jinja — the current recipes use
  none; if you add a parameterized recipe, wrap its `{{ }}` in `{% raw %}`.

## Toggles & gating

`project_type` (library|service) is the structural fork. Every optional feature is an
`include_*` bool, **all default `true`** except `include_mcp_server` (false). Toggles that only
apply to one archetype are `when:`-gated so they aren't even asked (e.g. `include_pypi_publish`
library-only; `include_docker_publish` / `include_hadolint` / `include_mcp_server` service-only).

Three gating mechanisms:
1. **Whole-file drop** via Jinja `_exclude` (file absent from the render) — for standalone files
   like `mkdocs.yml`, `.github/workflows/release.yml`, `src/{{ package_name }}/pretty.py`.
2. **In-file `{%- if include_X %}`** blocks — for partial edits to always-present files
   (`pyproject.toml.jinja` deps/extras/coverage, `compose.yml.jinja`).
3. **Conditional dir/path** named with Jinja.

**No-dead-var rule:** every declared `copier.yml` question must be consumed by a template file or
a `copier.yml` conditional. `no-dead-vars.sh` enforces it in CI. Adding a toggle = add the
question **and** wire it to real files in the same change.

## What each archetype renders

**Base (always):** `uv` + PEP 735 dependency-groups + `[tool.uv] required-version`; `ruff` broad
ruleset (`F,E,W,I,UP,B,SIM,C4,RUF,S,A,PT,RET,PTH,FA,TC,ARG,T20`, plus `UP046/UP047` ignored for
the 3.11 floor); `basedpyright` strict; strict `pytest` (`filterwarnings=["error"]`,
`asyncio_mode=auto`) + coverage `fail_under=80`; `pre-commit` (local ruff via uv); `justfile`;
`.editorconfig`/`.gitattributes`/`.vscode`; `.claude/` (settings + guardrail hooks + `commands/`);
base CI (`ci`/`audit`/`pre-commit`), hardened Dependabot; Dev Container + host ssh-agent scripts;
ADR scaffold; `CHANGELOG`; `UPGRADING`.

**Library tier:** hatchling + `src/{{ package_name }}/__version__.py`; optional-extras worked
example (`pretty`/`rich`); test tiers (unit always; `integration` testcontainers + `contract`
opt-in); shipped `testing/` helpers; mkdocs + Pages; PyPI Trusted-Publishing `release.yml`.

**Service tier (`project_type==service`):** FastAPI `create_app()` + fail-soft lifespan
(`/healthz` always 200, `/readyz` reports degraded); pydantic-settings `config.py`; multi-stage
non-root `Dockerfile` + `compose.yml`; GHCR `docker-publish.yml`; `hadolint.yml`; optional FastMCP
`mcp_server.py` **mounted at `/mcp`** (streamable-http; session manager run in the app lifespan,
`streamable_http_path="/"`, DNS-rebinding allowlist via `*_MCP_ALLOWED_HOSTS`).

## Conventions

- **Never commit to `main`.** Branch (`feat/…`, `fix/…`, `chore/…`), open a PR, let `render-test`
  pass, merge.
- **Tag `vX.Y.Z` for every consumer-visible change** — `copier copy gh:…` resolves the latest tag,
  so unmerged-to-a-tag changes don't reach consumers. Repo-only changes (this `CLAUDE.md`, docs)
  don't need a tag. SemVer by blast radius.
- **Fully-wired-or-flagged:** don't ship a toggle/file that only half-works; wire it end-to-end or
  flag the gap explicitly.
- **`repos/` is read-only reference.** Crib patterns; never edit; it never renders.
- Keep changes surgical and the variable contract stable.

## Release / versioning

Bump nothing in the template repo itself (it has no package version). Cut a release by: matrix
green → choose the SemVer bump → update consumer `CHANGELOG`/`_migrations` if breaking →
`git tag -a vX.Y.Z` → `git push origin vX.Y.Z`. Verify with a tagless
`copier copy gh:allada-homelab/python-template` (should report the new version).
