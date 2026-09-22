# Dev container

LLM Middleman ships a [dev container](https://containers.dev/) so the toolchain
(Python 3.14, `uv`, `just`, `pre-commit`, the GitHub CLI, a Docker
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

Git over SSH works from both, through your host's SSH agent — see [SSH](#ssh).

## What the definition guarantees

1. **No path spells the repository's directory name.** Everything uses
   `${containerWorkspaceFolder}` / `${localWorkspaceFolderBasename}` and
   workspace-relative script paths, so renaming or cloning the repo to a different
   directory does not break the container.
2. **Container state lives in named volumes, not on the bind mount.** `.venv` is a
   named volume (`llm-middleman-venv-${devcontainerId}`), because a venv shared
   with the host points at an interpreter that exists on only one side: host `uv`
   deletes and rebuilds it for host Python, container `uv` then does the same in
   reverse, and you pay a full resync on every switch. The agentic-CLI state
   directories and `~/.ssh` are per-worktree named volumes too — no token or
   transcript leaks back onto the host — and the uv cache is one volume shared by
   every worktree. Two writes do reach the host on purpose: `.devcontainer/initialize`
   (which runs *on* the host) writes git-ignored files under `.devcontainer/`, and in a
   normal checkout `post-create.sh` installs the pre-commit hooks into `.git/hooks`.
3. **Git works from a linked worktree**, not just the main checkout — see below.
4. **`remoteUser` is non-root** (`vscode`) with `"updateRemoteUserUID": true`, so files
   you create in the container are owned by *you* on the host.
5. **Feature versions are locked.** `devcontainer-lock.json` is committed; check it
   with `up --frozen-lockfile`, which fails when a feature has drifted from the pinned
   digests (this project's CI does not build the container). Dependabot's
   `devcontainers` ecosystem bumps features and the lock; regenerate it by hand by
   running `up` with the pinned CLI and committing the result. The base image tag
   (`3-3.14-trixie`) pins the image major and Debian release but still
   takes patch rebuilds. The uv feature's `version` option is pinned (its default
   floats to `latest`) and nothing bumps it automatically;
   keep it equal to the uv the `Dockerfile` copies in.
6. **The Docker socket comes from the `docker-outside-of-docker` feature's default
   mount** and nothing else. `.devcontainer/post-start.sh` reports which half is
   missing — socket not mounted, or daemon unreachable — and never claims the
   container is ready after a failure.
7. **The venv always matches `uv.lock`.** `uv sync --locked --all-groups --all-extras`
   runs on create (`updateContentCommand`), on every start (`post-start.sh`, warn-only)
   and after every checkout/merge (the `uv-sync` pre-commit hook). A stale lock is
   reported, never silently re-resolved.
8. **`python` is the uv-managed interpreter from `.python-version`** — in VS Code and
   under `devcontainer exec`. `post-create.sh` runs `uv python install --default`,
   `remoteEnv` puts `~/.local/bin` first on PATH, and `UV_PYTHON_PREFERENCE=only-managed`
   stops uv from picking the image's own Python. VS Code uses
   `${workspaceFolder}/.venv/bin/python`. A plain `docker exec` does not apply
   `remoteEnv`, so a bare `python` there is the image's; `uv run` is right everywhere.

## Git identity

Git in the container needs a `user.email`, or every commit — and every test that makes
one — dies with `unable to auto-detect email address`. It does **not** get that by
mounting your `~/.gitconfig`: on many machines that file is only an `[include]` /
`[includeIf]` shim pointing at a dotfiles checkout or a work/personal split, and those
paths are not mounted, so the include resolves to nothing in here.

Instead `.devcontainer/initialize` writes a **flattened** copy of your effective global
config — `git config --global --includes --list`, with the `include.*` / `includeif.*`
directives themselves dropped — to `.devcontainer/.gitconfig.host`, which is what
`devcontainer.json` bind-mounts read-only and `post-create.sh` pulls in with
`git config --global include.path`. It is rewritten on every `up`, so a change on the
host reaches the container on the next start, and it is git-ignored
(`.devcontainer/.gitignore`): it is host-specific and can hold credential settings.

Three things to know:

- Keys that name host binaries are **not** copied: `credential.helper` /
  `credential.<url>.helper` (VS Code forwards its own helper, and a copied
  `gh auth setup-git` block would wipe it with its blank `helper =`), `core.pager`,
  `pager.*` and `interactive.diffFilter` (delta and friends). Other `credential.*` keys,
  such as `useHttpPath`, still come across. Everything else is copied verbatim, and a
  valueless boolean (`[core] bare`) is copied as `true`.
- If `git config --global --includes --list` fails on the host, `initialize` stops
  rather than starting a container with no identity.
- `includeIf "gitdir:…"` conditions are evaluated against **this** repository, so a
  work/personal split gives the container the identity that repository would get on the
  host.

If the host has no `user.email` at all, `post-create.sh` says so and the container still
builds; set it on the host and recreate the container. `post-create.sh` sets
`core.autocrlf`, `core.eol`, `init.defaultBranch` and `core.editor` only when the host's
config does not already set them.

## SSH

The container gets your host's **SSH agent**, never your key files: nothing in here —
an agent in bypass mode included — can copy a private key out, only ask the agent to
sign. `~/.ssh` in the container is a per-worktree named volume, not your host `~/.ssh`,
so your host `~/.ssh/config` is not used either.

- **VS Code** forwards its own agent into the container automatically when one is
  running on the host.
- **The CLI does not forward anything** (`devcontainer exec`, `docker exec`, agents), so
  `devcontainer.json` bind-mounts the socket in `$SSH_AUTH_SOCK` at `/ssh-agent.sock`
  and sets `SSH_AUTH_SOCK` in `containerEnv`, where every process sees it.
  `.devcontainer/initialize` refuses to start when `SSH_AUTH_SOCK` is set but empty or
  names something that is not a live socket, and warns (then mounts `/dev/null`) when it
  is unset — fine for CI. No agent running on the host? Source the script in
  `host_setup_scripts/` (see `CONTRIBUTING.md`).
- **Stale socket.** The mount binds the socket *path* the host had when the container
  was created, re-resolved on every container start. If the host agent restarts at the
  same path, stop and start the container; if it comes back at a new path (a plain
  `ssh-agent -s` picks a new one each time), recreate it with `--remove-existing-container`.
  `post-start.sh` warns when the agent is dead or has no keys loaded.
- **Host keys.** github.com is verified against its published keys, pinned in
  `.devcontainer/ssh_known_hosts` (mounted at `/etc/ssh/ssh_known_hosts`; refresh it
  when GitHub rotates a key). Your host's `~/.ssh/known_hosts` is mounted read-only as a
  second trust file, and new hosts are learned into the volume's own `known_hosts`.
- **Commit signing** works through the agent with no host paths: set `gpg.format ssh`
  and `user.signingKey "key::ssh-ed25519 AAAA…"` (the public key, inline). Nothing here
  configures it for you.

## Docker socket (accepted risk)

The `docker-outside-of-docker` feature hands the container the host's **rootful** Docker
socket, and access to it is root on the host: any process in the container, including
an agent, can `docker run -v /:/host …`. Keeping key files out of the container does not
contain host secrets while the socket is present. It stays because integration tests and
`docker compose` need it; remove the feature if you do not.

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
