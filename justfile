set dotenv-load := true

# List available recipes.
default:
    @just --list

# Install deps (all groups + extras) and git hooks.
install:
    uv sync --locked --all-groups --all-extras
    uv run pre-commit install
    uv run pre-commit install --hook-type post-checkout --hook-type post-merge

# Sync deps to the lockfile.
sync:
    uv sync --locked --all-groups --all-extras

# Run unit tests.
test:
    uv run pytest

# Run tests with coverage and the configured fail-under gate.
coverage:
    uv run coverage run -m pytest
    uv run coverage report

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

# Verify the lockfile is current.
lock-check:
    uv lock --check

# Type-check.
typecheck:
    uv run basedpyright

# Run all pre-commit hooks.
pre-commit:
    uv run pre-commit run --all-files
