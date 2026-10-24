# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
