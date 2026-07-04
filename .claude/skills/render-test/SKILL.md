---
name: render-test
description: Test the Copier template while developing it — the commit-first render + gate loop, the render-test matrix cells, adding answer cells, debugging a failed render, and testing copier update (with the macOS symlink fix). Use after editing copier.yml or anything under template/ to verify renders, or when a render or a render-test cell fails.
---

# Render-testing the template

Copier renders from a **git ref**, so **commit first**, then render from `HEAD`.

## Fast path: the root `justfile`

A root `justfile` wraps the whole loop (renders into gitignored `.render/<cell>/`):

```bash
just check lib-on     # render + gate one cell
just render-dirty svc-on && just gate svc-on   # include uncommitted changes (throwaway wip commit)
just boot svc-on      # boot a service cell, probe /healthz, tear down (host-only)
just matrix           # render + gate every cell + no-dead-vars (what CI runs)
just clean            # rm -rf .render/
```

The raw commands below are what those recipes run — reach for them to debug a render.

## Single-cell render + gate

```bash
git add -A && git commit -m wip      # copier reads a ref, not the dirty tree
copier copy --trust --vcs-ref HEAD --defaults \
  --data-file .github/render-test/answers/lib-on.yml . /tmp/out
cd /tmp/out && uv sync --all-extras --all-groups
uv run ruff check . && uv run ruff format --check . && uv run basedpyright \
  && uv run pytest -q && uv run coverage run -m pytest && uv run coverage report
```

Service cells additionally: `docker compose config >/dev/null` (or full boot: `just up`, probe
`curl -fsS http://127.0.0.1:8000/healthz`, then `just down`).

## Full matrix (run before every push — this is what CI runs)

Cells in `.github/render-test/answers/`: `lib-on`, `lib-off`, `svc-on`, `svc-off`,
`lib-variations`, `svc-variations`. Render each from `HEAD` and run the gate; then:

```bash
bash .github/render-test/no-dead-vars.sh   # every copier question must be consumed
```

`.github/workflows/render-test.yml` runs the identical matrix on `ubuntu-latest`.

## Adding an answer cell

Drop a `<name>.yml` in `.github/render-test/answers/` (only the non-default answers), then add
`<name>` to the `matrix.cell` list in `.github/workflows/render-test.yml`. Use a cell to lock in a
non-default combination (e.g. `lib-variations`: Apache + `python_min=3.12` + extras-off + ubuntu).

## Debugging a failed render

- **Wrong/old content rendered** → you forgot `--vcs-ref HEAD` (copier used the latest tag) or
  didn't commit.
- **A copier var rendered empty** → it's a leading-underscore name (a setting, not a question).
- **A gated file appears when it shouldn't** → the `_exclude` pattern used the `.jinja` source name;
  it must match the **rendered destination** path.
- **Leaked `{% raw %}` / mangled `${{ }}`** in a workflow → fix the raw-wrapping/escape.
- **ruff/pyright/pytest failure** → reproduce inside the rendered `/tmp/out` and fix the template
  source, then re-render. (Common: `UP046`/`UP047` (PEP 695 generics) are ignored unconditionally
  for the 3.11 support floor; `filterwarnings=["error"]` promoting a dep warning — add a documented
  ignore.)

## Testing `copier update`

Automated — this is the one path the render matrix doesn't cover (a consumer's edits could be
silently clobbered on update). Run it via the justfile; CI runs it as the `update-test` job:

```bash
just update-test    # renders the latest release tag → HEAD, asserts a clean 3-way merge
```

It (`.github/render-test/test_update.py`, a PEP 723 pytest script driven by copier's Python API,
parametrized over library + service): renders the latest tag with tasks on → git-commits it →
applies a consumer edit to a template-managed file → `copier update` to HEAD → asserts no conflict
markers, the consumer edit survives, and `.copier-answers.yml` advanced. Needs real git tags
(`fetch-depth: 0` in CI). A file removed by a dropped question (e.g. `codeql.yml`) is deleted on
update — that's a clean update, and the test skips index entries the update removed from disk.

Run it from a **clean tree**: copier renders `vcs_ref=HEAD` including uncommitted template changes
(`DirtyLocalWarning`), so a dirty local tree can make the result differ from CI (which checks out
clean). Commit first if a local pass/fail looks surprising.

Manual debug loop (when a run fails and you want to inspect the tree):

```bash
OLD=$(cd "$(mktemp -d)" && pwd -P)     # macOS: resolve /tmp & /var symlinks or update errors
copier copy --trust --vcs-ref v<prev> --defaults --data project_name="Upd" "$(pwd)" "$OLD"
cd "$OLD" && git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm init
# (optionally add consumer edits + commit)
copier update --trust --vcs-ref HEAD --defaults .
```

Confirm exit 0, new template files pulled in, and consumer code untouched.
