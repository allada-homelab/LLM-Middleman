#!/usr/bin/env bash
# Runs once on create. The template repo has no pyproject to `uv sync` — its
# toolchain is copier (via uvx), just (rust-just uv tool), shellcheck (apt), pre-commit.
set -euo pipefail

# uv-tool shims land in ~/.local/bin, which the base image only adds to PATH via
# an interactive-shell rc snippet — lifecycle hooks don't always source it.
export PATH="$HOME/.local/bin:$PATH"

echo "==> Configuring git defaults..."
if [ -f /home/vscode/.gitconfig.host ]; then
    git config --global include.path /home/vscode/.gitconfig.host
fi
git config --global init.defaultBranch &>/dev/null || git config --global init.defaultBranch main
git config --global --add safe.directory '*'

echo "==> Installing shellcheck..."
sudo apt-get update -qq
sudo apt-get install -y -qq shellcheck

echo "==> Installing just + pre-commit as uv tools..."
uv tool install rust-just==1.55.1   # official just install path (casey/just README); ships the `just` binary
uv tool install pre-commit==4.6.0
pre-commit install --install-hooks   # warm hook envs now, not on the first commit

echo "==> Warming copier (uvx cache)..."
uvx copier --version

echo "==> Template dev container ready. Try: just --list"
