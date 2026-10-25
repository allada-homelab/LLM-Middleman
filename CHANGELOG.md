# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **The Timeout setting again bounds how long a backend may stay silent.** Since 1.1.4 any
  backend that sent nothing for 30 seconds failed, whatever its Timeout said: a blocking
  n8n reply, a slow model still processing the prompt, a reasoning model, or an Ollama model
  load. Timeout now covers both reaching the backend and every silence before or during
  the reply; a reply that keeps streaming is still never cut off. (#110)
- **n8n keepalive frames no longer break streaming.** n8n 2.4x sends a keepalive line
  every 30 seconds while a streamed reply is in progress; when one came first the reply
  failed as "not a JSON object". Keepalives are now ignored. (#110)
- **n8n replies in every shape n8n sends are spoken (LLMM-025).** A *Respond to Webhook* →
  Text reply was mistaken for a proxy error page and always fell back; *All Entries* /
  *All Incoming Items* array replies were rejected; a JSON reply sent with streaming on was
  read out as raw JSON; and several streamed segments ran together ("First part.Second
  part."). Text replies and arrays are now spoken, streamed JSON is unwrapped to its reply
  field, and segments are separated. A failing n8n node is now named in the log instead of
  "None".
- **The custom `/v1/converse` preset no longer passes off a cut-off reply as complete
  (LLMM-025).** A stream that ends without `done` keeps what was said, adds the fallback
  message and marks the turn failed. An empty `done` now gives the translated fallback,
  also marked failed.

### Added

- **n8n setup checks the webhook (LLMM-025).** Saving an n8n connection now refuses an
  inactive workflow's URL and `/webhook-test/` URLs with their own messages. The check
  cannot start a workflow run, Chat Triggers included.
- **The custom `/v1/converse` setup check rejects a wrong bearer token (LLMM-025)** as
  invalid authentication; before, any answer passed.
- **Backend tool activity in the Assist chat for n8n and custom `/v1/converse`
  (LLMM-025):** an n8n AI Agent's intermediate steps (non-streaming replies with *Return
  Intermediate Steps* on) and converse `tool_activity` events sent before the reply text.
  The converse contract documents an optional `: keepalive` comment, the time prefix on
  `text`, and the optional `tool_activity` `id`. Existing backends need no changes.

## [1.3.0] - 2026-10-25

### Added

- **AI Task `generate_data` (LLMM-023).** OpenAI-compatible and Ollama connections offer
  **Add AI task**. The new entity serves `ai_task.generate_data` with free text or a
  `structure`, sent as `response_format` `json_schema` (OpenAI-compatible) or Ollama's
  `format`. It accepts image attachments and runs the Home Assistant tool loop when the
  task passes an LLM API. Failures raise errors instead of returning fallback text; a
  rejected API key starts reauthentication. (#106)
- **README reference tables** for every preset's connection fields and every agent and
  AI task option, with defaults. (#108)

### Changed

- **Quieter logs while a backend is down.** A connection now logs one info line when its
  backend becomes unreachable and one when it answers again, shared by all of its agents
  and AI tasks. AI tasks that cannot reach the backend now also count toward the
  "Backend unreachable" repair issue.
  Turns that cannot reach the backend log at debug instead of an error with a traceback
  each time; failures after reaching the backend still log as errors. (#108)

## [1.2.0] - 2026-10-25

### Added

- **Reauthentication.** Rejected credentials at setup, or a 401/403 during a turn, now
  start Home Assistant's reauth flow, which asks only for the preset's credential
  field(s). You no longer have to delete and re-add the connection. (#100)
- **"Backend unreachable" repair issue.** Three turns in a row that cannot reach the
  backend raise a warning repair issue; it clears when the backend answers again.
  (#100)
- **Token usage in the conversation trace** for `openai_compat` and `ollama` (#98),
  LangGraph (#99) and Dify (#101). `openai_compat` asks for `stream_options.include_usage`
  and retries once without it on servers that reject the field.
- **Reasoning for `openai_compat`.** `reasoning_content` / `reasoning` deltas (DeepSeek,
  vLLM, llama.cpp, OpenRouter) appear as thinking in the Assist chat; they are never
  spoken. (#98)
- **Backend tool activity in the Assist chat** for LangGraph (#99) and Dify Agent apps
  and Chatflows (#101): tools the backend ran show up as tool calls and results.
- **LangGraph (LLMM-022):** a graph's `interrupt()` question is spoken, the satellite
  keeps listening, and the answer resumes the graph; new agent options *Double-texting*
  (default: roll back the superseded run), *Spoken node*, *Input messages key* and
  *Stateless runs*; a Bearer API-key header option on the connection; abandoned runs are
  cancelled; HA turn metadata is sent in the run context; a wrong assistant id fails at
  setup. (#99)
- **Dify (LLMM-021):** new-style Agent apps are accepted; new agent options *Dify app
  inputs*, *Send Home Assistant context* and *Use the Home Assistant user*; setup names
  the problem for a workflow-app key or an unpublished Agent; moderation replacements,
  reasoning and paused Chatflows are handled. (#101)
- **More agent options in the form:** temperature, top P and maximum tokens
  (`openai_compat`); context window, keep alive and think (`ollama`); message, reply and
  session field names (`n8n`). The adapters already honoured these, but the form never
  offered them. Left empty, the backend's default applies. (#102)

### Changed

- **Device memory scope is now per agent.** Two agents on one connection used to share a
  device's session. For Dify and LangGraph, an existing device session is kept by the
  first agent that talks on that device after the upgrade; others start fresh. n8n and
  custom `/v1/converse` device sessions start fresh once (the session key the backend
  sees changes). (#102)
- **Setup probes the backend.** A backend that is down now makes HA retry setup instead
  of loading and answering every turn with the fallback; adding an identical connection
  aborts as already configured. (#97)
- **Fallback replies are marked as errors** (intent error `unknown`, translated text set
  apart from any partial reply), so automations can tell an unreachable backend from a
  real answer. (#97)
- **The turn-time prefix is only added for presets that forward bare text** (custom
  `/v1/converse`, Dify). `openai_compat`, `ollama`, `n8n` and LangGraph get the time from
  HA's system prompt. (#97, #99)
- **Tool results carry an error flag** (`{"data", "error"}`, as HA's own integrations
  send them); undescribed tools no longer send `"description": null`. (#97)
- **LangGraph thread ids are deterministic** and created on first use; threads from
  v1.1.x keep their history. (#99)
- **Dify app inputs are redacted in diagnostics.** (#101)
- **SSE parsing is several times faster** for long or chunky streams. (#97)
- **Development:** HACS validation, a coverage gate and tag-driven GitHub releases in CI
  (#90); cross-preset adapter-contract and streaming end-to-end gates (#91); copier
  answers and the pyproject version now match the HACS layout and manifest (#92); shared
  adapter helpers (#96).

### Fixed

- **n8n HTTP errors fall back cleanly.** A 401/500 used to be parsed as a reply (an error
  body with an `output` field would have been spoken); it is now logged with its status.
  Dify's turn-path 401 also names its status. (#91)
- **Malformed backend responses no longer crash config forms.** A non-object probe body
  is a connection error instead of a crash (#96); an unexpected probe exception shows as
  an error on the form, and the model-list probe can no longer freeze the agent form for
  60 s (#97).
- **Unbounded memory use on misbehaving streams.** Ollama and n8n NDJSON lines are
  capped and n8n blocking replies are limited to 1 MiB. (#96)
- **One malformed tool call no longer fails an `openai_compat` turn**; the bad call is
  dropped and the rest of the reply kept. (#97)
- **Reconfiguring an entry whose setup failed now reloads it** (#100), and reconfigure
  no longer reloads twice (#97).
- **Dify:** a Chatflow cancelled before its first answer, or a timed-out turn, now stops
  the Dify task; an ended conversation is recreated instead of wedging the agent; a
  dropped stream is no longer passed off as a complete reply. (#101)
- **LangGraph no longer speaks tool output.** (#99)
- **Switching agents mid-conversation no longer confuses `openai_compat` / `ollama`.**
  Tool calls a LangGraph or Dify backend ran itself were replayed to the next model as
  if it had made them. (#102)
- **Removing a connection deletes all of its stored data**, including LangGraph's
  pending-interrupt state (previously only the session map). (#97, #102)
- **The README now explains how to remove the integration.** (#102)

## [1.1.4] - 2026-10-24

### Changed

- **Minimum Home Assistant is now 2026.7.0** (`hacs.json`). The test suite now runs
  against the latest HA (2026.10.3) and this floor in CI; older versions were never
  verified and the n8n basic-auth path already needed aiohttp ≥3.14. (#88)
- **The agent timeout now bounds connecting to the backend, not the whole reply.** (#84)

### Fixed

- **HA tool calling works again on HA ≥2026.9.** HA moved tool schemas to `probatio`,
  which `voluptuous-openapi` can't convert, so `openai_compat`/`ollama` sent broken tool
  definitions. Schemas are now converted the way HA's own integrations do, with a
  fallback for older HA; tool results and the v0 device migration also follow HA
  2026.10's renamed APIs. (#88)
- **Long streaming replies are no longer cut off.** A reply still producing tokens after
  the agent timeout (60 s; 30 s for n8n) was killed and the error message appended to
  the partial text. A backend that goes silent for 30 s is still dropped. (#84)
- **Dify: dispatch on the event name inside the JSON.** Dify sends bare `data:` frames,
  so mid-stream `error` events were swallowed, Agent apps spoke their answer twice, and
  `message_end`/`message_replace` were not recognized. (#86)
- **The agent prompt now reaches LangGraph and n8n.** They read a key nothing wrote; the
  HA-rendered prompt is now sent as `context.system_prompt` (LangGraph threads) or
  `systemPrompt` (n8n). (#87)
- **`openai_compat` logs and falls back on HTTP errors** instead of ending silently on a
  401/500. (#83)
- **Drop `aiohttp` from `manifest.json` requirements**, which current hassfest rejects as
  an HA core dependency. (#85)
- **Declare the `voluptuous-openapi` runtime dependency in `manifest.json`.** The
  `ollama`/`openai_compat` backends import `voluptuous_openapi`, but it was never
  declared in `requirements`. It previously arrived in the HA runtime transitively
  via `homeassistant`; when HA dropped it from its own deps (2026.9.x), the
  integration failed to import at load and the whole component went `unavailable`,
  taking the conversation agent (e.g. Dify "New Jarvis") down with it. Now declared
  explicitly. Also adds a regression guard test (`tests/test_manifest_requirements.py`)
  that asserts every third-party module the integration imports is declared in
  `manifest.json` `requirements` — independent of what `homeassistant` happens to
  provide, so a future HA dep-removal can't silently break the integration again.

## [1.1.3] - 2026-07-27

### Added

- **Prepend the current datetime (HA's configured timezone) to the user message before
  forwarding.** Gives Dify/backends temporal context ("now" is not a guess for the LLM)
  without leaking device state. (#50)

## [1.1.2] - 2026-07-23

### Fixed

- **Dify advanced-chat (chatflow) apps no longer fail intermittently on tool turns.** The SSE
  reader capped a single line at 64 KB and raised `BackendStreamError` on any line over it,
  aborting the whole turn — Home Assistant spoke the "could not reach the assistant" fallback.
  A Dify chatflow streams verbose `node_finished`/`agent_log` frames; a busy agent node's frame
  was measured at 130–145 KB, so tool-calling turns failed whenever that frame preceded the
  answer deltas (agent-chat apps, with small frames, were unaffected). The reader now drains an
  over-cap line to its terminator and skips it — it is verbose node metadata the Dify adapter
  already discards, never a content delta — keeping memory bounded (the cap's original purpose)
  while letting the stream continue to the answer. This is a strict improvement for every
  backend: an oversized frame that used to kill the turn is now survived. (#44)

## [1.1.1] - 2026-07-22

### Fixed

- **Dify: strip `<think>` reasoning from streamed answers.** Reasoning-model chain-of-thought
  (`<think>…</think>`) inlined in the Dify `answer` was forwarded to Home Assistant and spoken
  aloud by the voice pipeline. It is now stripped mid-stream — handling multiple blocks and tags
  split across deltas — so only the final reply reaches TTS. (#41)
- **Dify: recover from a stale/deleted conversation.** The stale-conversation recreate path now
  recognizes Dify's live `404` `{"code": "not_found", "message": "Conversation Not Exists…"}`
  response (previously only `conversation_not_exists` was matched), so a deleted or expired
  `conversation_id` is dropped and the turn retried once instead of wedging the agent on every
  turn. (#42)

## [1.1.0] - 2026-07-09

### Added

- **Dify backend preset.** First-class Dify support (Chatbot / Agent / Chatflow apps) over the
  streaming `chat-messages` API, with server-side conversation memory and transparent
  stale-conversation recreate. (#34)
- **Client-integration guide** for wiring LiteLLM agents / MCP tools to Home Assistant.

### Fixed

- Replace deprecated `aiohttp.BasicAuth` usage in the n8n backend.

### Internal

- Durable end-to-end regression rig under `scripts/e2e/`, plus scheduled CI and Dependabot to
  keep the integration from silently rotting.

## [1.0.0] - 2026-07-05

### Added

- **Multi-backend presets.** Pick a backend type in the config UI — **OpenAI-compatible**,
  **Ollama** (native), **LangGraph**, **Custom `/v1/converse`** (the v0 contract, now one
  preset), or **n8n** — each behind a common adapter layer with a spec-compliant streaming
  parser and a hardened "never hang the pipeline" guard.
- **Parent-entry + subentry config model.** A connection (backend type, URL, auth) is the
  parent entry; each conversation agent (name, system prompt, per-agent options) is a
  subentry, so one connection can host several agents.
- **Optional Home Assistant tool loop.** Per-agent `llm_hass_api` multi-select for
  tool-capable presets (OpenAI-compatible, Ollama), pulling in device control plus any HA
  MCP-client tools, with `ConversationEntityFeature.CONTROL` and a bounded iteration cap.
- **Per-agent memory scope** (`conversation` / `device` / `agent`) for stateful backends,
  and **per-agent timeouts** (total deadline plus an idle-read timeout).
- **Follow-up listening.** Automatic when a reply ends in a question mark (all presets),
  plus explicit override via `done.continue_conversation` (custom `/v1/converse`) and
  `continueConversation` (n8n).
- **Redacted config-entry diagnostics** (credentials, URLs, and prompts redacted).

### Changed

- Re-architected from the single-backend v0 shim (one bespoke `/v1/converse` SSE contract)
  into the preset model above. A v0 entry migrates in place to a Custom `/v1/converse`
  connection with one agent, preserving the existing entity id.
