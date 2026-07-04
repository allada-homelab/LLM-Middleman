set dotenv-load := true

# List available recipes.
default:
    @just --list

# Install deps (all groups) and git hooks.
install:
    uv sync --all-groups
    uv run pre-commit install
    uv run pre-commit install --hook-type post-checkout --hook-type post-merge

# Sync deps to the lockfile.
sync:
    uv sync --all-groups

# Run unit tests.
test:
    uv run pytest

# Run integration tests (requires a Docker daemon).
test-int:
    uv run pytest tests/integration -m integration

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

# Type-check.
typecheck:
    uv run basedpyright

# Run all pre-commit hooks.
pre-commit:
    uv run pre-commit run --all-files

# Build sdist + wheel.
build:
    uv build

# Validate the compose configuration.
compose-config:
    docker compose config

# Build and start the service stack (detached).
up:
    docker compose up -d --build

# Stop the service stack and remove volumes.
down:
    docker compose down -v

