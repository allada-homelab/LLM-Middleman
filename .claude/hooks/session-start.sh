#!/usr/bin/env bash
# SessionStart hook — generic project grounding.
#
# stdout is injected as session context. Bootstraps the environment if needed
# and prints a one-line git status. Fails OPEN everywhere — every probe degrades
# to a no-op or warning, never aborts the session.

set -euo pipefail

root=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
[ -z "$root" ] && exit 0

# Bootstrap deps on first run (fail-open: a sync failure must not block the session).
if [ ! -d "$root/.venv" ] && command -v uv >/dev/null 2>&1; then
    echo "- .venv missing — running 'uv sync --all-groups'..."
    uv sync --all-groups --project "$root" >/dev/null 2>&1 || echo "  WARNING: uv sync failed; run 'just install' manually."
fi

# Seed .env from the example if present and not yet created.
if [ -f "$root/.env.example" ] && [ ! -f "$root/.env" ]; then
    cp -n "$root/.env.example" "$root/.env" 2>/dev/null || true
    echo "- Seeded .env from .env.example."
fi

# One-line git status: branch + dirty-file count.
branch=$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
dirty=$( { git -C "$root" status --porcelain 2>/dev/null || true; } | wc -l | tr -d ' ')
echo "- git: branch '$branch', $dirty uncommitted file(s)."

exit 0
