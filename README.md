# LLM Middleman

[![Validate](https://github.com/allada-homelab/LLM-Middleman/actions/workflows/validate.yml/badge.svg)](https://github.com/allada-homelab/LLM-Middleman/actions/workflows/validate.yml)
[![Lint](https://github.com/allada-homelab/LLM-Middleman/actions/workflows/lint.yml/badge.svg)](https://github.com/allada-homelab/LLM-Middleman/actions/workflows/lint.yml)

A text-only **Home Assistant conversation agent** (HACS custom integration) that forwards
each Assist/Voice turn to an **external LLM backend** and streams the reply back into the
pipeline. It runs **no LLM of its own** — intelligence (the model, memory, and any
server-side tools) lives in the backend. Home Assistant keeps the voice front-end; the
integration owns only the plumbing.

Instead of speaking one bespoke wire protocol, you pick a **backend preset** in the config
UI, enter a URL and (optional) auth, and add one or more conversation *agents* under it.

## Backend presets

Pick one preset per connection. Each streams the reply token-by-token
(`_attr_supports_streaming = True`) so streaming TTS can start speaking early.

| Preset | Talks to | Transport | HA tool loop |
|--------|----------|-----------|--------------|
| **OpenAI-compatible** | any `/v1/chat/completions` server (llama.cpp, vLLM, LiteLLM, Ollama's OpenAI mode, …) | SSE, `[DONE]` sentinel | Yes |
| **Ollama** | a native Ollama server (`/api/chat`) | NDJSON, `done: true` | Not yet¹ |
| **LangGraph** | a LangGraph deployment (`langgraph dev` / self-hosted / cloud) | SSE, `messages-tuple` | No² |
| **Custom `/v1/converse`** | your own external agent speaking the v0 SSE contract | SSE, `text_delta`/`done`/`error` | No² |
| **n8n** | an n8n Chat Trigger / Webhook workflow | NDJSON `StructuredChunk` (with a blocking fallback) | No² |

¹ The Ollama adapter's native protocol carries tool calls; wiring them into the HA tool
loop is tracked separately and not enabled in this build.
² These backends run their own tools server-side — the integration passes text through and
does not expose Home Assistant's tools to them.

## Configuration model

Configuration follows Home Assistant's parent-entry + subentries pattern (the same model
core's OpenAI and Ollama integrations use):

- **The parent entry is a *connection*** — backend type plus URL and auth. Adding the
  integration walks you through a backend-type dropdown and then that backend's connection
  form, validated by a live probe against the real endpoint.
- **Each *conversation* subentry is an agent** — name, system prompt, and per-agent options
  (model where the backend has a catalog, timeout, and more). One agent = one conversation
  entity you can assign under **Settings → Voice assistants**. One connection can host
  several agents with different prompts or memory settings.

Per-agent options that appear only when the chosen backend supports them:

- **Home Assistant tools (`llm_hass_api`)** — a multi-select of the LLM APIs (device
  control plus any tools from HA's MCP-client integration) whose tools the backend may
  call. Offered only for tool-capable presets (see the table above). Selecting at least one
  makes the agent claim device commands (`ConversationEntityFeature.CONTROL`) so the
  pipeline's local-intent fallback does not also handle them.
- **Memory scope** — for stateful backends (LangGraph, custom `/v1/converse`, n8n),
  controls the session key sent to the backend: `conversation` (default; the HA
  conversation, reset after HA's ~5-minute session timeout), `device` (one long-lived
  thread per satellite/room), or `agent` (one continuous thread for the whole agent).
  Stateless backends (OpenAI-compatible, Ollama) replay Home Assistant's chat log instead,
  so this option is hidden for them.
- **Timeout** — a per-agent total deadline (paired with an idle-read timeout so a slow but
  live stream is not killed). On a timeout or backend error the agent speaks a graceful
  fallback rather than hanging the pipeline.

## Follow-up listening

Home Assistant keeps the microphone open for a wake-word-free follow-up when the assistant
reply ends in a question mark — this works automatically for **every** preset. Two presets
can also request it explicitly: the custom `/v1/converse` preset honors
`done.continue_conversation`, and n8n honors a `continueConversation` field in a blocking
reply. The follow-up turn keeps the same conversation id, so backend context carries across
the clarify → answer loop.

## A note on local intents

Sentence triggers and (when enabled) `prefer_local_intents` intercept a turn **before** any
conversation agent runs, and Home Assistant's intent stage handles timers and simple device
commands ahead of the agent. So a matched local intent (a timer, "turn on the lights")
never reaches this integration — that is expected, not a bug. If your agent "never got the
message," check whether a local sentence/intent claimed the turn first.

## Migration from v0

The pre-preset v0 build (a single flat entry speaking `/v1/converse`) upgrades in place:
its entry is migrated to a **Custom `/v1/converse`** connection with one conversation agent,
and the existing entity id is preserved so automations and Assist exposure keep working.

## Diagnostics

The integration ships redacted config-entry diagnostics (**Settings → Devices & Services →
… → Download diagnostics**). Credentials, URLs, and prompts are redacted; backend type,
entry state, and entity metadata are included to aid support without leaking secrets.

## Installation (HACS)

1. Add this repository as a custom repository in HACS (category: Integration).
2. Install **LLM Middleman** and restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → LLM Middleman**.
4. Pick a backend type, then fill in that backend's connection form (URL + auth).
5. Add a conversation agent (name, system prompt, per-agent options).
6. Assign the agent under **Settings → Voice assistants**.

## Docs

- `docs/knowledge/03-the-shim.md` — the multi-backend design (architecture, config model,
  the preset matrix, follow-up listening, voice latency).
- `docs/external-agent-handoff/` — build spec for an external agent that implements the
  custom `/v1/converse` preset (one optional preset, not the boundary contract).
- `docs/implementation/plan.md` — the v1 architecture of record.

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
