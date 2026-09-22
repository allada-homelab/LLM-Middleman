#!/usr/bin/env bash
set -euo pipefail

echo "==> Configuring git defaults..."
if [ -f /home/vscode/.gitconfig.host ]; then
    git config --global include.path /home/vscode/.gitconfig.host
fi
# Warn, never fail: a host with no identity configured should still get a
# container. But say so here, because the symptom otherwise turns up much later
# as "unable to auto-detect email address" from a commit or a test.
git config --get user.email > /dev/null ||
    echo "post-create: WARNING no git user.email — set it on the host (git config --global user.email) and rerun devcontainer up --remove-existing-container" >&2
# --includes: a --global read otherwise ignores the include.path above, so these
# "only if unset" guards would always overwrite the host's own values.
git config --global --includes --get core.autocrlf &>/dev/null || git config --global core.autocrlf input
git config --global --includes --get core.eol &>/dev/null || git config --global core.eol lf
git config --global --includes --get init.defaultBranch &>/dev/null || git config --global init.defaultBranch main
git config --global --includes --get core.editor &>/dev/null || git config --global core.editor "vim"
git config --global --add safe.directory '*'
# The container mounts the git common dir but not the host paths the sibling
# worktrees live at, so a linked worktree that is alive and well on the host still
# reads as `prunable` in here. `git gc` prunes worktrees, and this is the only knob
# that stops it. (`git worktree prune` ignores this setting entirely — never run it
# in here.)
git config --global gc.worktreePruneExpire never

echo "==> Configuring SSH..."
chmod 700 ~/.ssh
# New host keys are learned into the writable volume file (the first one listed);
# hosts the host machine already trusts come from its read-only known_hosts, and
# github.com from the pinned /etc/ssh/ssh_known_hosts. Written once: the volume
# persists, so later hand edits are kept.
if [ ! -e ~/.ssh/config ]; then
    printf 'UserKnownHostsFile ~/.ssh/known_hosts ~/.ssh/known_hosts.host\n' > ~/.ssh/config
    chmod 600 ~/.ssh/config
fi
# Warn, never fail: VS Code forwards its own agent regardless of this one.
[ -S /ssh-agent.sock ] ||
    echo "post-create: WARNING no host SSH agent mounted (SSH_AUTH_SOCK unset on the host at create time) — ssh from devcontainer exec / the CLI has no key. Start one on the host (host_setup_scripts/) and rerun devcontainer up --remove-existing-container" >&2

echo "==> Verifying git works in this workspace..."
# The point of the .gitcommon/.gitentry mounts is a container where git genuinely
# works, not one that merely starts. `set -e` makes a broken setup fail here,
# loudly, rather than surfacing later as a wrong answer from a gate run.
git rev-parse --git-dir > /dev/null
git status --porcelain > /dev/null
echo "    git OK: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(no commits yet)')"

echo "==> Installing the pinned Python (.python-version) as the default python..."
# --default puts python/python3 symlinks in ~/.local/bin (on PATH via remoteEnv),
# so a bare `python` in here is the uv-managed interpreter the venv is built on,
# not the image's own.
uv python install --default

echo "==> Installing Python dependencies (all dev groups + extras)..."
# --all-extras matters: CI syncs with it, so without it an optional-extra import
# typechecks red in here and green in CI, which reads as a broken container.
# --locked: a stale uv.lock fails here instead of being silently re-resolved.
uv sync --locked --all-groups --all-extras

echo "==> Installing just as a uv tool..."
# The justfile is the documented dev loop, so `just` has to exist in the container
# that runs it. Pinned uv tool rather than a community feature — same approach the
# template repo's own dev container uses.
uv tool install rust-just==1.55.1

echo "==> Installing pre-commit hooks..."
# A hooks dir we cannot write to means the container user's uid does not match the
# host's on the bind mount. Say that, rather than letting pre-commit fail with a
# bare EACCES. `--git-path hooks` is load-bearing: in a worktree `.git` is a file
# and the hooks live in the shared common dir, not `./.git/hooks`.
hooks_dir=$(git rev-parse --path-format=absolute --git-path hooks)
if [ ! -w "$hooks_dir" ]; then
    echo "post-create: $hooks_dir is not writable (uid mismatch between host and container user; see the devcontainer contract)" >&2
    exit 1
fi

# Git hooks live in the COMMON dir, which a linked worktree shares with the parent
# repository and every sibling worktree. Installing from in here would rewrite that
# one shared hook to point at ${containerWorkspaceFolder}/.venv/bin/python, a path
# that exists on no host; pre-commit's generated hook then exits 1 for the host and
# for every sibling worktree at once. Only install when the hooks we would write are
# this workspace's own.
workspace=$(git rev-parse --show-toplevel)
common_dir=$(git rev-parse --path-format=absolute --git-common-dir)
case "$common_dir" in
    "$workspace"/*)
        uv run pre-commit install
        uv run pre-commit install --hook-type post-checkout
        uv run pre-commit install --hook-type post-merge
        ;;
    *)
        echo "    Skipped: this is a linked worktree, and its hooks live in"
        echo "    $common_dir, shared with the parent repository. Commit from the"
        echo "    host, where the installed hook's interpreter actually exists."
        ;;
esac

echo "==> Dev container setup complete!"
