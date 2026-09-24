---
name: update-project
description: Update this project to the latest version of the Copier template it was generated from (copier update). Use when the user wants to pull template improvements/upgrades into this project, sync with the upstream template, or run copier update. See UPGRADING.md for details.
---

# Update this project from its template

This project was generated with [Copier](https://copier.readthedocs.io/); `.copier-answers.yml`
records the source template, the answers, and the version. `copier update` re-renders against a
newer template version and three-way-merges the result into the working tree. Requires `copier`
(`uv tool install copier` or `uvx copier`).

## Steps

1. **Clean tree first.** `git status` — commit or stash any changes. `copier update` refuses to run
   on a dirty tree, and a clean baseline makes the merge reviewable.
2. **Update:**
   ```bash
   copier update --trust --defaults
   ```
   - `--defaults` is required for non-interactive (agent) runs — without it copier prompts for any
     unanswered question and fails on a non-TTY stdin. A human in a real terminal may omit it.
   - `--trust` is required because the template declares tasks/migrations (Copier blocks them
     otherwise). The `_tasks` themselves only run on first copy, not on update (see step 4).
   - To re-answer the questions, run it in a terminal without `--defaults`; to skip ones already
     answered, add `--skip-answered` (`-A`).
3. **Resolve conflicts.** Edited files that the template also changed produce normal merge
   conflicts or `.rej` reject files — resolve them like a git merge, then delete any `.rej` files.
   Files in the template's `_skip_if_exists` (`uv.lock`, `README.md`, `CHANGELOG.md`) are left
   untouched.
4. **Re-sync deps.** Post-generation tasks (e.g. `uv sync`) only run on first copy, not on update:
   ```bash
   uv sync --all-extras --all-groups
   ```
5. **Run the gate** before committing:
   ```bash
   just lint && just typecheck && just test
   ```
6. **Commit** the update (the bumped `.copier-answers.yml` plus merged changes).

## Escape hatch

If an update is too tangled to merge, `copier recopy --trust` re-renders from scratch with the
current answers (overwrites template-managed files) — use sparingly, then reconcile.

## Breaking changes

Template releases that need manual steps are noted in the template's release notes; some are
automated via Copier `_migrations` and applied during `copier update`. See `UPGRADING.md`.
