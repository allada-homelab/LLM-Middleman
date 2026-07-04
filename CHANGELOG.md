# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Multi-backend presets.** Pick a backend type in the config UI — **OpenAI-compatible**,
  **Ollama** (native), **LangGraph**, **Custom `/v1/converse`** (the v0 contract, now one
  preset), or **n8n** — each behind a common adapter layer with a spec-compliant streaming
  parser and a hardened "never hang the pipeline" guard.
- **Parent-entry + subentry config model.** A connection (backend type, URL, auth) is the
  parent entry; each conversation agent (name, system prompt, per-agent options) is a
  subentry, so one connection can host several agents.
- **Optional Home Assistant tool loop.** Per-agent `llm_hass_api` multi-select for
  tool-capable presets (OpenAI-compatible), pulling in device control plus any HA
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
