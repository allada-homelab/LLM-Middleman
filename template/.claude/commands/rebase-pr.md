---
description: Rebase the current PR branch onto the latest main, safely
---

Rebase the current branch onto `origin/main`:

1. `git fetch origin main`.
2. Confirm the branch is **not** `main` and the working tree is clean.
3. `git rebase origin/main`. If conflicts arise, resolve them, then re-run the gate:
   `just lint && just typecheck && just test`.
4. Push with `git push --force-with-lease` (**never** bare `--force`).
5. If an open PR exists for the branch, leave a one-line comment noting the rebase.

Stop and report if the conflicts are non-trivial rather than guessing at resolutions.
