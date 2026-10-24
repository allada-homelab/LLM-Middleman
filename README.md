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
| **Ollama** | a native Ollama server (`/api/chat`) | NDJSON, `done: true` | Yes |
| **LangGraph** | a LangGraph deployment (`langgraph dev` / self-hosted / cloud) | SSE, `messages-tuple` + `updates` | No¹ |
| **Custom `/v1/converse`** | your own external agent speaking the v0 SSE contract | SSE, `text_delta`/`done`/`error` | No¹ |
| **n8n** | an n8n Chat Trigger / Webhook workflow | NDJSON `StructuredChunk` (with a blocking fallback) | No¹ |
| **Dify** | a Dify chat / agent / chatflow app (`/chat-messages`) | SSE, `agent_message`/`message`/`message_end` | No¹ |

¹ These backends run their own tools server-side — the integration passes text through and
does not expose Home Assistant's tools to them. LangGraph's tool calls and results are shown
in the Assist chat log as *external* tool activity (never executed by Home Assistant, never
spoken).

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
- **Memory scope** — for stateful backends (LangGraph, custom `/v1/converse`, n8n, Dify),
  controls the session key sent to the backend: `conversation` (default; the HA
  conversation, reset after HA's ~5-minute session timeout), `device` (one long-lived
  thread per satellite/room), or `agent` (one continuous thread for the whole agent).
  Stateless backends (OpenAI-compatible, Ollama) replay Home Assistant's chat log instead,
  so this option is hidden for them.
- **Timeout** — a per-agent total deadline (paired with an idle-read timeout so a slow but
  live stream is not killed). On a timeout or backend error the agent speaks a graceful
  fallback rather than hanging the pipeline.

### How the agent prompt reaches each backend

The agent's system prompt is a Home Assistant template. It is rendered each turn (with
HA's date/time and any `extra_system_prompt`) and then forwarded:

- **OpenAI-compatible / Ollama** — as the leading `system` message of the replayed history.
- **n8n** — as a `systemPrompt` field in the webhook body; wire it into your AI Agent node.
- **LangGraph** — on persistent threads, as the run's `context.system_prompt` (read it in
  the graph via `runtime.context.system_prompt`, as the official `react-agent` template's
  `Context` does); it is never appended to thread state. With *stateless runs* it is the
  leading `system` message of the run input instead. See [LangGraph](#langgraph) for the
  rest of the run context.
- **Custom `/v1/converse` / Dify** — not forwarded; the backend owns its prompt.

## Follow-up listening

Home Assistant keeps the microphone open for a wake-word-free follow-up when the assistant
reply ends in a question mark — this works automatically for **every** preset. Two presets
can also request it explicitly: the custom `/v1/converse` preset honors
`done.continue_conversation`, and n8n honors a `continueConversation` field in a blocking
reply. The follow-up turn keeps the same conversation id, so backend context carries across
the clarify → answer loop. LangGraph requests it when the graph pauses on `interrupt()` (see
below).

## LangGraph

Graph-side contract and behaviour of the LangGraph preset, verified against a live
`langgraph-api` 0.16.6 server (`docs/implementation/e2e-results/langgraph-tier1.md`):

- **Run context.** Every run's `context` carries `system_prompt` (persistent threads only),
  `language`, `device_id`, `satellite_id`, `conversation_id`, and the HA `user_id`; keys
  without a value are omitted. Read them via `runtime.context`. A dataclass
  `context_schema` drops keys it does not declare, so declare only what the graph needs.
- **Threads.** Each memory-scope session maps to a deterministic thread id
  (`uuid5(<entry_id>:<session key>)`) created on first use (`if_not_exists: create`), so a
  thread you delete server-side is simply re-created empty on the next turn. Threads made
  by v1.1.x for `device`/`agent` scopes keep being used.
- **What is spoken.** Only AI message text; tool calls, tool results and echoed input are
  never sent to TTS. *Spoken node* restricts speech to one graph node.
- **Interrupts.** A graph that pauses on `interrupt(value)` has its question spoken (a
  string value, or a `question`/`message` key), the satellite keeps listening, and the
  next utterance resumes the thread with `Command(resume=<utterance>)`.
- **Double-texting.** *Double-texting* (`multitask_strategy`) decides what a new turn does
  to a reply still running on the same thread. The default, `rollback`, cancels the old run
  and removes it from history, so its unanswered utterance never lingers. `interrupt`
  keeps its partial progress, `enqueue` waits for it (the new turn can take as long as the
  old one), and `reject` refuses the new turn (the fallback is spoken).
- **Cancellation.** Runs are started with `on_disconnect: cancel`, and a turn Home
  Assistant abandons (cancelled, timed out) also sends a best-effort
  `POST /threads/{id}/runs/{run_id}/cancel`, so the graph stops working on it.
- **Long tool steps.** The server sends an SSE heartbeat every 5 s, so a silent tool step
  longer than the 30 s idle timeout does not cut the reply off.
- **Auth.** The API key goes in the `x-api-key` header (LangSmith cloud; the default) or,
  with *API key header* set to Bearer, as `Authorization: Bearer …` for custom auth
  handlers. Setup checks that the assistant/graph id exists on the deployment.
- **Token usage.** `usage_metadata` from AI messages is written to the conversation trace.

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
entry state, and entity metadata are included to aid support without leaking secrets. A
loaded LangGraph connection also reports its last run id.

## Installation (HACS)

Requires Home Assistant **2026.7.0** or newer.

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
