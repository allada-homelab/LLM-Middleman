---
description: Create an isolated git worktree + branch for feature work
---

Set up an isolated worktree for new work (branch name from `$ARGUMENTS`, else infer it):

1. Confirm the current tree is clean (`git status`); stash or commit first if not.
2. Choose a branch name with a `feat/`, `fix/`, or `chore/` prefix. Never use `main`.
3. Create a **sibling** worktree (outside this repo dir):
   `git worktree add ../$(basename "$PWD")-<branch> -b <branch>`
4. `cd` into it and install: `just install` (or `uv sync --all-groups`).
5. Report the worktree path and branch.

Do not create the worktree inside the repo. Remove it when done with
`git worktree remove <path>`.
