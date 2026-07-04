# LLM Middleman

[![CI](https://github.com/allada-homelab/LLM-Middleman/actions/workflows/ci.yml/badge.svg)](https://github.com/allada-homelab/LLM-Middleman/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Passthrough middleman service bridging Home Assistant Assist/Voice to an external LLM agent

## Development

This project uses [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/).

```bash
just install     # uv sync --all-groups + pre-commit install
just test        # run unit tests
just lint        # ruff check
just fmt         # ruff format
just typecheck   # basedpyright
```

> Generated from [allada-homelab/python-template](https://github.com/allada-homelab/python-template).
> Re-render with `copier update`.

## GitHub setup after generation

Some features need a one-time GitHub setting before their workflows go green:

- **CI runner** — workflows run on `${{ vars.CI_RUNNER || 'homelab-runners' }}`. If this repo has no self-hosted runners, set a `CI_RUNNER` repository/organization variable (e.g. `ubuntu-latest`) or CI won't start.
- **Docs (Pages)** — Settings → Pages → Source: **GitHub Actions** (the `docs` workflow deploys there).
- **Discussions** — Settings → General → Features → enable **Discussions** (for the question-form template).
- **PR labeler** — the labels referenced in `.github/labeler.yml` must exist (create them under Issues → Labels).
- **GHCR image** — `docker-publish` pushes to GHCR via `GITHUB_TOKEN`; make the package public in the repo's Packages settings if you want anonymous pulls.
