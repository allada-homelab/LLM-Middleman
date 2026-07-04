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

echo "==> Installing just (task runner)..."
uv tool install rust-just

echo "==> Installing Python dependencies (all dev groups)..."
uv sync --all-groups

echo "==> Installing pre-commit hooks..."
uv run pre-commit install
uv run pre-commit install --hook-type post-checkout
uv run pre-commit install --hook-type post-merge

echo "==> Dev container setup complete!"
