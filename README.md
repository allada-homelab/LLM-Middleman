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
| **Dify** | a Dify Chatbot / Agent / Legacy Agent / Chatflow app (`/chat-messages`) | SSE, `agent_message`/`message`/`message_end` | No¹ |

¹ These backends run their own tools server-side — the integration passes text through and
does not expose Home Assistant's tools to them. LangGraph's and Dify's tool calls and results
(Dify: Agent tool steps and Chatflow tool nodes) are shown in the Assist chat log as *external* tool activity (never executed by Home Assistant, never
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
- **Each *AI task* subentry is an AI Task entity** (OpenAI-compatible and Ollama
  connections only). See [AI Task](#ai-task-generate_data).

Per-agent options that appear only when the chosen backend supports them:

- **Home Assistant tools (`llm_hass_api`)** — a multi-select of the LLM APIs (device
  control plus any tools from HA's MCP-client integration) whose tools the backend may
  call. Offered only for tool-capable presets (see the table above). Selecting at least one
  makes the agent claim device commands (`ConversationEntityFeature.CONTROL`) so the
  pipeline's local-intent fallback does not also handle them.
- **Memory scope** — for stateful backends (LangGraph, custom `/v1/converse`, n8n, Dify),
  controls the session key sent to the backend: `conversation` (default; the HA
  conversation, reset after HA's ~5-minute session timeout), `device` (one long-lived
  thread per satellite/room, separate for each agent), or `agent` (one continuous thread
  for the whole agent).
  Stateless backends (OpenAI-compatible, Ollama) replay Home Assistant's chat log instead,
  so this option is hidden for them.
- **Dify options** — *Dify app inputs* maps the app's input variables (a YAML mapping such
  as `city: Berlin`; required variables must be set, and the form lists the variables the
  app declares). *Send Home Assistant context* fills the app's optional input variables
  `ha_language`, `ha_device_id`, `ha_area`, `ha_satellite_id`, and `ha_extra_system_prompt`
  (only those the app declares); declare `ha_extra_system_prompt` as a paragraph input so a
  satellite's announced question (`start_conversation` / `ask_question`) reaches the app on
  the follow-up "yes". *Use the Home Assistant user* sends the HA user id as the Dify end
  user (voice-satellite turns keep the shared `home-assistant` identity).
- **Timeout** — how long to wait to reach the backend, and the longest it may stay silent:
  before its reply starts (a blocking reply, a slow model, a model load) or between
  streamed chunks. There is no total deadline: a reply that keeps streaming is never cut
  off, however long it runs. Raise it for slow backends. On
  a timeout or backend error the agent speaks a graceful fallback rather than hanging the
  pipeline.

Every connection field and agent option is listed in
[Connection parameters](#connection-parameters) and [Agent and AI task options](#agent-and-ai-task-options).

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

## Connection parameters

Adding the integration first asks for the **Backend type** (the presets above), then shows
that preset's connection form. Saving probes the real endpoint, so a wrong URL or
credential fails right there. **Reconfigure** reopens the same form (the backend type is
fixed), and when the backend later rejects the credentials, **reauthentication** asks for
the credential fields only. Two connections are duplicates only if every field matches,
so one URL can back several connections (for example Dify apps that differ only by key).

| Preset | Field | Required | Meaning |
|--------|-------|----------|---------|
| OpenAI-compatible | Base URL | Yes | Server root without a path. A trailing `/v1` or slash is stripped, since `/v1/...` is added for each request. |
| | API key | No | Sent as `Authorization: Bearer <key>`. Many "OpenAI-compatible" servers need a non-empty dummy key. |
| Ollama | Base URL | Yes | Ollama host root, for example `http://host:11434`. Not the `/v1` path. A trailing slash is stripped. |
| | API key | No | Sent as `Authorization: Bearer <key>`. |
| LangGraph | Deployment URL | Yes | The LangGraph deployment URL (`langgraph dev`, self-hosted, or cloud). A trailing slash is stripped. |
| | API key | No | Needed for LangSmith cloud deployments and servers with custom auth. |
| | API key header | Yes (default `x-api-key`) | How the key is sent: the `x-api-key` header (LangSmith cloud and the default for self-hosted servers) or `Authorization: Bearer` (custom auth handlers). |
| | Assistant ID | Yes (default `agent`) | Graph name or assistant UUID. Checked against the deployment when you save. |
| Custom `/v1/converse` | Base URL | Yes | Backend URL; `/v1/converse` is appended. A trailing slash is stripped. |
| | Bearer token | No | Sent as `Authorization: Bearer <token>`. |
| n8n | Webhook URL | Yes | The workflow's full production URL (a Chat Trigger's `…/webhook/<id>/chat`). Stored exactly as entered. |
| | Target type | Yes (default Chat Trigger) | *Chat Trigger*, or *Webhook* for a plain Webhook node answered by a Respond to Webhook node. |
| | Authentication | Yes (default None) | *None*, *Basic auth*, or *Custom header*, matching the trigger node's authentication. |
| | Username, Password | With basic auth | The basic-auth credentials. |
| | Header name, Header value | With a custom header | The header the workflow checks, sent on every request. |
| Dify | Base URL | Yes | Dify API root including `/v1` (Dify Cloud: `https://api.dify.ai/v1`). `/v1` is appended if you leave it out. |
| | API key | Yes | The app's API key (starts with `app-`). It must belong to a Chatbot, Agent, or Chatflow app; Workflow and Text Generator keys, and unpublished Agents, are rejected when you save. |

## Agent and AI task options

Each **conversation agent** (Add conversation agent) has these options. The form shows only
the ones the connection's preset uses. "Default" is the value the form starts with.

| Option | Presets | Default | Meaning |
|--------|---------|---------|---------|
| Name | All | `LLM Middleman` | Name of the agent's device and conversation entity. Asked when you add the agent only. |
| System prompt | All | Home Assistant's default instructions | Agent instructions, as a template. Forwarded as described in [How the agent prompt reaches each backend](#how-the-agent-prompt-reaches-each-backend); Custom `/v1/converse` and Dify ignore it. |
| Model | OpenAI-compatible, Ollama | none | Model from the server's catalog, or a typed name (a text field when the catalog can't be loaded). When unset, no model is sent and the server's default applies. |
| Max history | All (used by OpenAI-compatible and Ollama) | `20` | Past user turns replayed with each request. `0` replays the whole conversation. The other presets keep history on the backend and ignore it. |
| Timeout (seconds) | All | `60` (10–300) | How long to wait to reach the backend, and the longest it may stay silent before or while replying; past that the fallback is spoken. A streaming reply is never cut off. If the field is cleared, n8n waits 30 seconds and the other presets 60. |
| Memory scope | LangGraph, Custom `/v1/converse`, n8n, Dify | Per conversation | Which turns share backend memory: *Per conversation* (HA's conversation, which ends after about 5 idle minutes), *Per device* (one thread per satellite, separate for each agent), or *Per agent* (one continuous thread). |
| Control Home Assistant | OpenAI-compatible, Ollama | none | HA LLM APIs whose tools the model may call (device control, MCP-client tools). Empty means no tools and no device control. |
| Temperature | OpenAI-compatible | server default | Sampling temperature, 0–2. |
| Top P | OpenAI-compatible | server default | Nucleus sampling cutoff, 0–1. |
| Maximum tokens | OpenAI-compatible | server default | Longest reply, in tokens. Sent as `max_tokens`; a model that rejects it (OpenAI reasoning models) gets `max_completion_tokens` instead. |
| Reasoning effort | OpenAI-compatible | not sent | `reasoning_effort` for reasoning models: none, minimal, low, medium, high, extra high, maximum, or a typed value. Leave it empty for other models, which may reject it. |
| Context window | Ollama | `8192` | `num_ctx`, at least 2048 tokens. Lower it to save RAM, raise it for long conversations or many exposed entities. Empty sends 8192, like Home Assistant's Ollama integration. |
| Keep alive | Ollama | `-1` | Seconds the model stays loaded after a request; `-1` keeps it loaded, `0` unloads it at once. Empty sends `-1`, like Home Assistant's Ollama integration (this overrides the server's `OLLAMA_KEEP_ALIVE`). |
| Think before responding | Ollama | model default | *On*, *Off*, or a level (*Low*, *Medium*, *High*, *Maximum*) for models that take one. The reasoning appears in the chat log and is never spoken. |
| Double-texting | LangGraph | Roll back | What a new turn does to a run still going on the same thread: *Roll back*, *Interrupt*, *Enqueue*, or *Reject* (see [LangGraph](#langgraph)). |
| Input messages key | LangGraph | `messages` | Graph state key the new message list is sent under. |
| Spoken node | LangGraph | empty (every node) | Speak only AI messages produced by this graph node. |
| Stateless runs | LangGraph | off | Run each turn without a server-side thread, so the graph keeps no memory between turns. |
| Message field | n8n | `chatInput` | Request body field holding the user's message. Must match the Chat Trigger's chat input key. |
| Reply field | n8n | `output` | Field of a non-streaming reply holding the answer; `text` is tried when it is missing. |
| Session field | n8n | `sessionId` | Request body field holding the session key that n8n memory nodes partition on. |
| Dify app inputs | Dify | none | Values for the app's input variables, as a mapping such as `city: Berlin`. Required variables must be set; the form lists the variables the app declares. |
| Send Home Assistant context to Dify | Dify | off | Fill the app's `ha_language`, `ha_device_id`, `ha_area`, `ha_satellite_id`, and `ha_extra_system_prompt` input variables from each turn (only those the app declares). |
| Use the Home Assistant user as the Dify user | Dify | off | Send the HA user id as the Dify end user. Voice-satellite turns have no user and keep the shared `home-assistant` identity. |

Each **AI task** (Add AI task, OpenAI-compatible and Ollama only) has a smaller set:

| Option | Presets | Default | Meaning |
|--------|---------|---------|---------|
| Name | Both | `LLM Middleman AI Task` | Name of the AI task's device and entity. Asked when you add the task only. |
| Model | Both | none | As for agents. Use a vision model for image attachments. |
| Timeout (seconds) | Both | `60` (10–300) | As for agents, except that a silent backend fails the task instead of speaking a fallback. |
| Temperature, Top P, Maximum tokens, Reasoning effort | OpenAI-compatible | server default / not sent | As for agents. |
| Context window, Keep alive, Think before responding | Ollama | `8192` / `-1` / model default | As for agents. |

## AI Task (`generate_data`)

OpenAI-compatible and Ollama connections also offer **Add AI task**. Each AI task
subentry becomes an AI Task entity for Home Assistant's `ai_task.generate_data` action, so
automations can ask the same server for text or structured data without a second
integration. The other presets forward a turn to a backend that owns its own prompt and
output, so they can't enforce a structure and don't offer AI tasks.

- **Options:** model (from the server's catalog, or typed), timeout, and the preset's
  sampling/runtime options (OpenAI-compatible: temperature, top P, max tokens, reasoning effort; Ollama:
  context window, keep alive, think). There's no prompt or memory option. Each task
  brings its own instructions and runs as a fresh single-turn chat.
- **Structured output:** with a `structure`, the schema is sent as
  `response_format: {type: json_schema, strict: false}` (OpenAI-compatible) or as
  Ollama's `format`. The reply is parsed and returned as an object. If the reply isn't
  valid JSON, the action fails with an error and the reply is logged. Some
  OpenAI-compatible servers (older vLLM or LM Studio builds, some gateways) reject
  `json_schema`. The action then fails with the server's error, which is also logged.
  Use a server or model that supports structured outputs.
- **Attachments:** only images are supported, and the model must have vision. Images are
  sent base64-encoded with no size cap, so the server's request-size limit applies. Any
  other file type fails the action.
- **Home Assistant tools:** when the caller passes an LLM API (`llm_api`) with the task,
  the model can call its tools. The tool loop has the same limit as conversation turns.
- **Errors:** an unreachable backend, an empty reply, or a tool loop that hits its limit
  fails the action with an error. Unlike a conversation agent, there is no spoken
  fallback.

## OpenAI-compatible and Ollama

- **Errors mid-reply.** An error the server sends after the reply started (vLLM's error
  frame, OpenRouter's `finish_reason: "error"`, Ollama's final `{"error"}` line) keeps any
  text already spoken and adds the fallback message, and the turn is marked failed.
- **Cut-off replies.** A reply stopped by the token limit (`length`) or a content filter
  keeps its text. A warning is logged and `finish_reason` is written to the conversation
  trace.
- **Refusals.** An OpenAI-compatible model's `refusal` is spoken as its answer.
- **Rate limits.** On OpenAI-compatible servers, HTTP 429 (rate limit or exhausted quota) fails the turn with an error
  naming the server's reason (spoken as the fallback in a conversation, raised by AI tasks).
- **Prompt caching.** OpenAI-compatible requests carry `prompt_cache_key` set to the agent's
  id, so all of an agent's conversations share the provider's prompt cache. A server that
  rejects `prompt_cache_key` or `stream_options` gets the request again without it.
- **Token usage.** Input, cached-input, output and (OpenAI) reasoning tokens are written to
  the conversation trace.
- **Tools on Ollama.** Tool calls keep Ollama's call id, and tool results are sent back with
  the tool name and call id, so models that run several tools at once can match them.

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
  by v1.1.x for `device`/`agent` scopes keep being used (a v1.1.x `device` thread stays with
  the first agent that talks on that device).
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
  longer than the agent's Timeout does not cut the reply off.
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
entry state, entity metadata, and every conversation-agent and AI-task subentry are
included to aid support without leaking secrets. A
loaded LangGraph connection also reports its last run id.

## Installation (HACS)

Requires Home Assistant **2026.7.0** or newer.

1. Add this repository as a custom repository in HACS (category: Integration).
2. Install **LLM Middleman** and restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → LLM Middleman**.
4. Pick a backend type, then fill in that backend's connection form (URL + auth).
5. Add a conversation agent (name, system prompt, per-agent options).
6. Assign the agent under **Settings → Voice assistants**.

## Removal

1. Under **Settings → Voice assistants**, move any assistant using an LLM Middleman agent to
   another conversation agent.
2. **Settings → Devices & Services → LLM Middleman → ⋮ → Delete** for each connection. This
   also deletes the integration's stored conversation/thread mappings for that connection;
   history kept on the backend itself (Dify conversations, LangGraph threads) is not touched.
3. Optionally uninstall **LLM Middleman** in HACS and restart Home Assistant.

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
