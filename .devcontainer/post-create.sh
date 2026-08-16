#!/usr/bin/env bash
set -euo pipefail

echo "==> Configuring git defaults..."
if [ -f /home/vscode/.gitconfig.host ]; then
    git config --global include.path /home/vscode/.gitconfig.host
fi
git config --global core.autocrlf &>/dev/null || git config --global core.autocrlf input
git config --global core.eol &>/dev/null || git config --global core.eol lf
git config --global init.defaultBranch &>/dev/null || git config --global init.defaultBranch main
git config --global core.editor &>/dev/null || git config --global core.editor "vim"
git config --global --add safe.directory '*'

echo "==> Installing Python dependencies (all dev groups + extras)..."
# --all-extras matters: CI syncs with it, so without it an optional-extra import
# typechecks red in here and green in CI, which reads as a broken container.
uv sync --all-groups --all-extras

echo "==> Installing just as a uv tool..."
# The justfile is the documented dev loop, so `just` has to exist in the container
# that runs it. Pinned uv tool rather than a community feature — same approach the
# template repo's own dev container uses.
uv tool install rust-just==1.55.1

echo "==> Installing pre-commit hooks..."
uv run pre-commit install
uv run pre-commit install --hook-type post-checkout
uv run pre-commit install --hook-type post-merge

echo "==> Dev container setup complete!"
