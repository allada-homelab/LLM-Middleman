# Dev loop for working ON this Copier template (not the rendered output — that
# ships its own template/justfile.jinja). Renders answer cells into .render/<cell>/
# and runs the same gate CI runs. Copier reads a git *ref*, so `render` uses HEAD:
# commit first, or use `render-dirty` to include your uncommitted changes.
#
# This file is at the repo root, never under template/, so it is NOT rendered
# (_subdirectory: template) and can use just-native {{ }} without {% raw %}.

# Cells discovered from the checked-in answer files — single source of truth,
# so adding answers/<name>.yml automatically joins `just matrix`.
cells := `ls .github/render-test/answers/*.yml | xargs -n1 basename | sed 's/\.yml$//' | tr '\n' ' '`
render_dir := ".render"

# Show available recipes
default:
    @just --list

# Render one cell from HEAD into .render/<cell>/ (commit first — copier reads a ref)
render cell:
    rm -rf "{{render_dir}}/{{cell}}"
    uvx copier copy --trust --vcs-ref HEAD --defaults \
      --data-file .github/render-test/answers/{{cell}}.yml . "{{render_dir}}/{{cell}}"

# Render a cell INCLUDING uncommitted changes (throwaway wip commit, always undone)
render-dirty cell:
    #!/usr/bin/env bash
    # Smooths copier's commit-first tax: commit everything, render, then soft-reset
    # back — restoring the pre-commit state with changes staged. --no-verify skips hooks.
    set -euo pipefail
    if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
        echo "Tree clean — plain render."
        exec {{just_executable()}} render {{cell}}
    fi
    orig=$(git rev-parse HEAD)
    trap 'git reset -q --soft "$orig"' EXIT
    git add -A
    git commit -q --no-verify -m "wip: render-dirty {{cell}}"
    {{just_executable()}} render {{cell}}

# Run the full quality gate inside an already-rendered cell (mirrors CI exactly)
gate cell:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{render_dir}}/{{cell}}"
    uv sync --all-extras --all-groups
    uv run ruff check .
    uv run ruff format --check .
    uv run basedpyright
    uv run pytest -q
    uv run coverage run -m pytest
    uv run coverage report
    case "{{cell}}" in svc*) docker compose config >/dev/null && echo "compose config: OK";; esac

# Render + gate a single cell
check cell: (render cell) (gate cell)

# Render + gate EVERY cell, then assert no dead vars (this is what CI runs)
matrix:
    #!/usr/bin/env bash
    set -euo pipefail
    for cell in {{cells}}; do
        echo "==> $cell"
        {{just_executable()}} render "$cell"
        {{just_executable()}} gate "$cell"
    done
    {{just_executable()}} no-dead-vars
    echo "All cells green."

# Assert every copier.yml question is consumed by template/ or a copier.yml conditional
no-dead-vars:
    bash .github/render-test/no-dead-vars.sh

# Automated `copier update` test: latest release tag → HEAD, assert a clean merge
update-test:
    uv run --script .github/render-test/test_update.py

# Boot a rendered service cell, probe /healthz, then tear it down (host-only —
# in-container, compose bind paths + published ports resolve to the host)
boot cell:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{render_dir}}/{{cell}}"
    trap '{{just_executable()}} down' EXIT   # set before `up` so a failed start still tears down
    {{just_executable()}} up
    for _ in $(seq 1 30); do
        if curl -fsS http://127.0.0.1:8000/healthz; then echo " healthz: OK"; exit 0; fi
        sleep 1
    done
    echo "healthz never came up" >&2; exit 1

# Remove all rendered output
clean:
    rm -rf {{render_dir}}
