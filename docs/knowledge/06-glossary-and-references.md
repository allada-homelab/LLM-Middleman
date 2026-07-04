# 06 — Glossary, References & Live-Verification List

---

## 1. Glossary

**Home Assistant / Assist**
- **Assist** — HA's voice/text assistant framework (pipeline + conversation agents + intents).
- **Assist pipeline** (`assist_pipeline`) — the stages: wake word → STT → intent (local intents /
  conversation agent) → TTS.
- **Conversation agent / `ConversationEntity`** — the "brain" plugged into the pipeline's intent
  stage. Our **shim** is one. Extends `RestoreEntity`.
- **`AbstractConversationAgent`** — ABC mixin required by `async_set_agent`'s type hint.
- **`ChatLog`** — HA's per-turn conversation-state object; **owns tool execution** and loop
  continuation. Content types: `SystemContent`, `UserContent`, `AssistantContent`,
  `ToolResultContent`.
- **`async_add_delta_content_stream`** — the method you feed streamed deltas into; it accumulates
  text/thinking, extends tool calls, and auto-executes non-`external` tools.
- **`AssistantContentDeltaDict`** — the delta shape (`{role, content, thinking_content, tool_calls,
  native}`) the shim emits per streamed chunk.
- **`conversation_id`** — HA session key (ULID; ~5-min TTL). Passed to the middleman for session
  state.
- **`continue_conversation`** — computed flag to keep the mic open (assistant text ends in `?`/`;`).
- **Intent / `IntentTool`** — HA action primitive; `AssistAPI` turns each exposed intent
  (`HassTurnOn`, `HassLightSet`, …) into an LLM tool.
- **Exposed to Assist** — the per-entity toggle that gates which entities/tools the LLM (and
  `mcp_server`) can see. The control **safety boundary**.
- **`AI Task` (`ai_task`)** — one-shot, stateless generation service (`ai_task.generate_data`), fresh
  `ChatSession` each call; the home for structured-output / autonomous jobs.
- **`AITaskEntity` / `AITaskEntityFeature`** — base class + feature flags (`GENERATE_DATA`,
  `SUPPORT_ATTACHMENTS`, `GENERATE_IMAGE`).
- **Streaming TTS / `TTSAudioRequest`/`TTSAudioResponse`** — a TTS entity that accepts streamed text
  chunks (`message_gen`) and returns streamed audio (`data_gen`); since Voice Chapter 11 (2025.10)
  gives ~0.5 s time-to-first-audio.
- **`PipelineEvent` / `INTENT_PROGRESS` / `INTENT_END`** — the Assist pipeline's event stream; the
  agent's `ChatLog` deltas surface as `INTENT_PROGRESS`, the final reply as `INTENT_END`.
- **`ha-assist-chat` (`ha-assist-chat.ts`)** — the frontend Assist chat component; renders the pipeline
  event stream — this is what makes the built-in chat show the shim's conversation.
- **STT streaming** — HA STT accepts a streamed audio input but returns a **single final transcript**
  (no interim words).

**LLM helper framework**
- **`llm.API` / `AssistAPI` / `APIInstance`** — HA's tool-exposure abstraction. `AssistAPI`
  (`"assist"`) exposes HA intents as tools.
- **`llm.Tool` / `llm.ToolInput`** — tool definition / a tool call. `ToolInput.external` gates whether
  `ChatLog` auto-executes it.
- **`CONF_LLM_HASS_API`** — config key selecting which `llm.API`s an agent uses (imported from
  `homeassistant.const`).
- **`voluptuous_openapi.convert`** — turns a voluptuous/selector schema into JSON Schema for the LLM.

**MCP**
- **MCP (Model Context Protocol)** — protocol for exposing tools/resources to LLMs.
- **`mcp_server`** (stock HA) — makes HA an MCP **server** (exposes Assist tools over SSE, bearer
  auth). The middleman connects to this.
- **`mcp`** (stock HA) — makes HA an MCP **client** (wraps remote MCP tools as `llm.Tool`s).

**This project**
- **Shim** — the HA-side passthrough `ConversationEntity` (forwards turns; no LLM). **This repo**,
  `LLM-Middleman`, domain `llm_middleman`; a HACS integration.
- **External agent ("the brain"; "the middleman" in older prose)** — the *separate* service the shim
  forwards to (agent loop, LLM providers, MCP client to HA). Its own repo; spec in the brief. **Not
  this repo**, despite the repo name.
- **Provider / adapter** — per-backend wire-format translator (OpenAI-compatible, Anthropic); used by
  the external agent, not the shim.

**Backends**
- **llama-swap / Ollama / vLLM / LiteLLM** — OpenAI-compatible self-hosted LLM servers.
- **Chat Completions vs Responses API** — two OpenAI wire shapes; compatible backends speak Chat
  Completions; the Responses API is OpenAI-proper and stateful.

**Distribution**
- **HACS** — Home Assistant Community Store; **one integration per repository**.
- **hassfest** — HA's manifest/strings/brands validator.
- **quality scale** (`quality_scale.yaml`) — Bronze→Platinum self-assessment of integration maturity.

---

## 2. References (primary sources)

**HA developer docs**
- Conversation entity — https://developers.home-assistant.io/docs/core/entity/conversation/
- LLM API (tools) — https://developers.home-assistant.io/docs/core/llm/
- AI Task entity — https://developers.home-assistant.io/docs/core/entity/ai-task/
- Assist pipelines — https://developers.home-assistant.io/docs/voice/pipelines/
- Assist satellite entity — https://developers.home-assistant.io/docs/core/entity/assist-satellite/
- Config entries — https://developers.home-assistant.io/docs/config_entries_index/
- Integration manifest — https://developers.home-assistant.io/docs/creating_integration_manifest/
- Integration quality scale — https://developers.home-assistant.io/docs/core/integration-quality-scale/
- Testing — https://developers.home-assistant.io/docs/development_testing/
- Internationalization — https://developers.home-assistant.io/docs/internationalization/core/
- Brands proxy / local brand folder — https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/

**HA core source (dev branch)**
- `conversation/chat_log.py` — https://github.com/home-assistant/core/blob/dev/homeassistant/components/conversation/chat_log.py
- `conversation/entity.py` — https://github.com/home-assistant/core/blob/dev/homeassistant/components/conversation/entity.py
- `helpers/llm.py` — https://github.com/home-assistant/core/blob/dev/homeassistant/helpers/llm.py
- `helpers/chat_session.py` — https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/helpers/chat_session.py
- Reference integrations: `ollama/`, `openai_conversation/`, `anthropic/`,
  `google_generative_ai_conversation/` (each has `entity.py`, `conversation.py`, `ai_task.py`,
  `config_flow.py`)
- MockChatLog + PR #138112 — https://github.com/home-assistant/core/pull/138112

**HA user docs**
- `mcp_server` (HA as MCP server) — https://www.home-assistant.io/integrations/mcp_server/
- `mcp` (HA as MCP client) — https://www.home-assistant.io/integrations/mcp/
- Wyoming — https://www.home-assistant.io/integrations/wyoming/
- AI Task — https://www.home-assistant.io/integrations/ai_task/
- 2025.8 "summer of AI" — https://www.home-assistant.io/blog/2025/08/06/release-20258/

**Architecture / ecosystem**
- Config Subentries (arch #1070) — https://github.com/home-assistant/architecture/discussions/1070
- Standardize ChatSession (arch #1191) — https://github.com/home-assistant/architecture/discussions/1191
- TTS streaming (disc #2277) — https://github.com/orgs/home-assistant/discussions/2277
- Streaming TTS shipped — Voice Chapter 11 (2025.10) — https://www.home-assistant.io/blog/2025/10/22/voice-chapter-11/
- Streaming TTS entity API (arch #1205) — https://github.com/home-assistant/architecture/discussions/1205
- Speech-to-speech / "Speech Processors" **rejected** (arch #1223) — https://github.com/home-assistant/architecture/discussions/1223
- Built-in chat: `conversation/http.py` (chat_log subscribe) — https://github.com/home-assistant/core/blob/dev/homeassistant/components/conversation/http.py
- Built-in chat: `ha-assist-chat.ts` — https://github.com/home-assistant/frontend/blob/dev/src/components/ha-assist-chat.ts
- HACS integration publishing — https://www.hacs.xyz/docs/publish/integration/
- pytest-homeassistant-custom-component — https://github.com/MatthewFlamm/pytest-homeassistant-custom-component

**Sibling repo (prior implementation + provider prior-art)**
- Full HA research report — `LLM-Home-Controller/docs/research/ha-2026.7-rewrite-research.md`
  (mirrored here as `01-home-assistant-reference.md`).
- Working provider adapters — `LLM-Home-Controller/custom_components/llm_home_controller/providers/`.

---

## 3. Verification status (against HA 2026.7)

**Resolved this project:**
- ✅ **Audio passthrough (Mode A) is infeasible** in Assist 2026.7 — no audio-in/audio-out extension
  point; maintainers rejected pipeline complexity (arch #1223). → text-only.
- ✅ **Streaming TTS gives ~0.5 s TTFA** (Voice Chapter 11, 2025.10+) when the agent streams deltas.
- ✅ **The built-in Assist chat renders the forwarding shim for free** (ChatLog → `INTENT_PROGRESS`/
  `INTENT_END` → `ha-assist-chat.ts`); the "inject a turn without being the agent" bypass is
  unsupported but moot for text-only.
- ✅ **`async_provide_llm_data` is the right call** (`async_update_llm_data` now raises) — used in the
  built shim; type-checks.

**Still open (verify against a running HA, or when building the external agent):**
- **Real-HA voice smoke test** of the built shim (full STT → shim → TTS path) — NOT run.
- **hassfest** on the shim manifest — NOT run here (no HA core checkout); runs in `validate.yml` CI.
- **`mcp_server` SSE endpoint + auth** (application-credentials vs long-lived token) — for the
  external agent's MCP client.
- **Which local OpenAI-compatible backends** honor tool-calling / `response_format: json_schema` — for
  the external agent.
- **Anthropic extended-thinking signature replay** — for the external agent.
- **`continue_conversation`** — v1 propagates it. HA computes it automatically from the reply ending
  in `?` (all presets), and the entity additionally ORs in an explicit adapter flag: the custom
  `/v1/converse` preset honors `done.continue_conversation` and n8n honors `continueConversation`
  in a blocking reply (see `03` §6). OpenAI-compatible / Ollama / LangGraph have no protocol slot, so
  they rely on the automatic `?`-detection.
