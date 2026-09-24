---
name: rebase-pr
description: Bring the current PR branch up to date with the latest main, force-pushing only with explicit confirmation
user-invocable: true
disable-model-invocation: true
---

Update the current branch with `origin/main` (hard rule 2: no force-push without explicit confirmation):

1. `git fetch origin main`.
2. Confirm the branch is **not** `main` and the working tree is clean.
3. If the branch has **never been pushed** (`git ls-remote --exit-code --heads origin <branch>` fails),
   `git rebase origin/main`. Otherwise it is published: `git merge origin/main`. Rebase a published
   branch only if the user explicitly asks — confirm first, then push with `--force-with-lease`
   (never bare `--force`).
4. If conflicts arise, resolve them, then re-run the gate: `just lint && just typecheck && just test`.
5. `git push` (plain push after a merge or a first-time rebase; add `-u origin <branch>` on first push).
6. If an open PR exists for the branch, leave a one-line comment noting the update.

Stop and report if the conflicts are non-trivial rather than guessing at resolutions.
