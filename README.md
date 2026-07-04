# LLM Middleman

[![Validate](https://github.com/allada-homelab/LLM-Middleman/actions/workflows/validate.yml/badge.svg)](https://github.com/allada-homelab/LLM-Middleman/actions/workflows/validate.yml)
[![Lint](https://github.com/allada-homelab/LLM-Middleman/actions/workflows/lint.yml/badge.svg)](https://github.com/allada-homelab/LLM-Middleman/actions/workflows/lint.yml)

A thin, text-only **Home Assistant conversation agent** (HACS custom integration) that
forwards each Assist/Voice turn to an **external LLM agent** and streams the reply back
into the pipeline. It is a *passthrough shim*: it owns Home Assistant plumbing, not
intelligence. The external agent runs the LLM, calls tools, and holds memory — none of
that lives in Home Assistant.

## How it works

Per turn, the entity:

1. Receives the recognized utterance from the Assist pipeline.
2. POSTs `{conversation_id, text, language, device_id?}` to `<endpoint>/v1/converse`
   with an optional `Authorization: Bearer <token>`.
3. Consumes the `text/event-stream` response and translates each `text_delta` event
   into a Home Assistant assistant delta, so TTS can start speaking early
   (`_attr_supports_streaming = True`).
4. On an `error` event or a timeout, speaks a graceful fallback message instead of
   hanging the pipeline.

The shim exposes **no** Home Assistant tools itself — the external agent is responsible
for all tool calling (e.g. via Home Assistant's `mcp_server`). See
`docs/knowledge/03-the-shim.md` for the design and `docs/plans/middleman-implementation-brief.md`
for the full `/v1/converse` contract.

## Installation (HACS)

1. Add this repository as a custom repository in HACS (category: Integration).
2. Install **LLM Middleman** and restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **LLM Middleman**.
4. Enter the external agent's base URL (the `/v1/converse` path is appended
   automatically), an optional bearer token, a display name, and an optional system
   prompt override.
5. Select the new agent under Settings → Voice assistants.

## Development

This project uses [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/).

```bash
just sync        # uv sync --locked --dev
just test        # run tests
just lint        # ruff check
just fmt-check   # ruff format --check
just typecheck   # basedpyright
just check       # full CI gate (lockfile + lint + format + tests)
```
