# Upgrading

This project was generated from
[allada-homelab/python-template](https://github.com/allada-homelab/python-template)
with [Copier](https://copier.readthedocs.io/). Pull template improvements with
`copier update`.

## How it works

`.copier-answers.yml` (committed, **never hand-edited**) records the answers and the
template version this project was rendered from. `copier update` re-renders against a
newer template version using those answers and three-way-merges the result into your
working tree.

## Routine update

```bash
# Make sure the working tree is clean and committed first.
copier update --trust
```

- `--trust` is required because the template runs post-generation tasks
  (`uv sync`, `ruff format`, and on first copy `.env` bootstrap).
- Review the diff, run the gate (`just lint && just typecheck && just test`), commit.

## Re-answering questions

```bash
copier update --trust --defaults=false   # re-prompt every question
```

## Conflicts

Files you've edited that the template also changed produce standard merge conflicts
(or `.rej` reject files for non-mergeable hunks). Resolve them as you would a git
merge, delete any `.rej` files, then commit. Files listed under the template's
`_skip_if_exists` (e.g. `uv.lock`, `.env`, `README.md`, `CHANGELOG.md`) are never
overwritten.

## Heavy escape hatch

```bash
copier recopy --trust   # re-render from scratch with current answers (overwrites)
```

Use only when an update is too tangled to merge — it discards template-side history
for the affected files.

## Breaking changes

Template releases that require manual steps (renames, moved files) are called out in
the template's release notes / CHANGELOG. Some are automated via Copier `_migrations`
and run during `copier update`; the rest are documented per release.
