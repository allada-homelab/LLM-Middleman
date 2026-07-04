# List available recipes.
default:
    @just --list

# Sync deps (dev group) to the lockfile.
sync:
    uv sync --locked --dev

# Lint.
lint:
    uv run ruff check .

# Lint and autofix.
lint-fix:
    uv run ruff check --fix .

# Format.
fmt:
    uv run ruff format .

# Check formatting without writing.
fmt-check:
    uv run ruff format --check .

# Run tests.
test:
    uv run pytest tests/

# Verify the lockfile is current.
lock-check:
    uv lock --check

# Type-check.
typecheck:
    uv run basedpyright

# Run all pre-commit hooks.
pre-commit:
    uv run pre-commit run --all-files

# Run the full CI gate (lockfile + lint + format + tests).
check: lock-check lint fmt-check test
