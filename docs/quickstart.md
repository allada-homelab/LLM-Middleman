# Quickstart

## Install

```bash
pip install llm-middleman
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add llm-middleman
```

## Basic usage

```python
import llm_middleman

print(llm_middleman.__version__)
```

See the [API Reference](api-reference.md) for the full public surface.

## Run the service

```bash
just up            # build + start the stack (detached)
curl http://localhost:8000/healthz
just down          # stop and clean up
```

Secrets are file-mounted from `secrets/` at `/run/secrets/<name>` and read by
`llm_middleman.config.Settings` — never baked into the image or env.

## Develop

```bash
just install       # uv sync + git hooks
just test          # unit tests
just lint && just typecheck
```

See `CONTRIBUTING.md` for the full workflow.

