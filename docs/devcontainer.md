# Dev container

LLM Middleman ships a [dev container](https://containers.dev/) so the toolchain
(Python 3.13, `uv`, `just`, `pre-commit`, the GitHub CLI, a Docker
socket) is identical on every machine. This page is the contract it satisfies — read
it before changing `.devcontainer/`.

## Using it

VS Code: *Reopen in Container*. From a terminal, the CLI does the same thing:

```bash
npx -y @devcontainers/cli@0.87.0 up --workspace-folder .
npx -y @devcontainers/cli@0.87.0 exec --workspace-folder . -- just test
```

`--workspace-folder` takes the **host** path. Never prefix these with `DOCKER_HOST=…`:
the container needs the rootful daemon, and `.devcontainer/initialize` refuses to run
against a rootless one because its uid remapping makes everything written through the
bind mount land owned by a phantom user.

VS Code forwards your SSH agent into the container; **the CLI does not**. Commit and
push from the host, or from a VS Code terminal.

## What the definition guarantees

1. **No path spells the repository's directory name.** Everything uses
   `${containerWorkspaceFolder}` / `${localWorkspaceFolderBasename}` and
   workspace-relative script paths, so renaming or cloning the repo to a different
   directory does not break the container.
2. **Nothing a lifecycle command writes lands on the bind mount.** `.venv` is a named
   volume (`llm-middleman-venv-${devcontainerId}`), because a venv shared with
   the host points at an interpreter that exists on only one side: host `uv` deletes
   and rebuilds it for host Python, container `uv` then does the same in reverse, and
   you pay a full resync on every switch. The uv cache and the agentic-CLI state
   directories are named volumes for the same reason — plus no token or transcript
   leaks back onto the host.
3. **Git works from a linked worktree**, not just the main checkout — see below.
4. **`remoteUser` is non-root** (`vscode`) with `"updateRemoteUserUID": true`, so files
   you create in the container are owned by *you* on the host.
5. **Feature versions are locked.** `devcontainer-lock.json` is committed; CI validates
   it with `--frozen-lockfile`. Regenerate it by running `up` with the pinned CLI and
   committing the result.
6. **The Docker socket comes from the `docker-outside-of-docker` feature's default
   mount** and nothing else. `.devcontainer/post-start.sh` reports which half is
   missing — socket not mounted, or daemon unreachable — and never claims the
   container is ready after a failure.

## Git worktrees

In a linked worktree (`git worktree add ../feature-x`), `.git` is a *file* holding an
absolute pointer into the parent repository's `.git/worktrees/<name>` — a path outside
the workspace bind mount. Git inside the container would die during repository
discovery and `onCreateCommand` would exit 128 before anything else ran.

`devcontainer.json` mounts cannot branch on host state, so `.devcontainer/initialize`
runs on the host first and always produces the same two paths:

| path | normal checkout | linked worktree |
| --- | --- | --- |
| `.devcontainer/.gitcommon` → `/gitcommon` | symlink to `.git` | symlink to the parent's `.git` |
| `.devcontainer/.gitentry` → `<workspace>/.git` | symlink to `.git` (a no-op re-mount) | a file reading `gitdir: /gitcommon/worktrees/<name>` |

Both are git-ignored (`.devcontainer/.gitignore`): they hold absolute host paths.

Two consequences inside the container:

- **Never run `git worktree prune`.** The container can see the git common dir but not
  the host paths its sibling worktrees live at, so every one of them reads as
  *prunable* in here. `post-create.sh` sets `gc.worktreePruneExpire never`, which stops
  `git gc` from removing them — but a bare `git worktree prune` ignores that setting
  and deletes the metadata immediately.
- **`pre-commit` hooks are not installed from a worktree.** Hooks live in the shared
  common dir, so installing them in here would repoint the parent repository's and
  every sibling's hook at a `.venv` interpreter that exists in no host. `post-create.sh`
  detects this and skips, telling you to commit from the host.

## When the container fails to build

A failed `onCreateCommand` / `postCreateCommand` is **sticky**: the CLI writes its
marker *before* running the command, so a plain `up` on the next attempt skips the
step that failed and hands you a half-built container. Recover by recreating it:

```bash
npx -y @devcontainers/cli@0.87.0 up --workspace-folder . --remove-existing-container
```

Two failures worth recognising by their message:

- `post-create: … /hooks is not writable` — the container user's uid does not match
  yours on the bind mount. Check that `updateRemoteUserUID` is still `true` and that
  the daemon is rootful.
- `initialize: … is not a git working tree` — run `git init` (and make a first commit)
  before starting the container; the definition mounts the repository's git directory.
