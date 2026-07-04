# LLM Middleman v1 — Multi-Backend Conversation Agent Re-architecture

> **Provenance:** approved by the owner on 2026-07-04 after a research-backed planning
> session (six Opus research agents; claims verified against the installed HA 2026.7.1
> source and live protocol docs). This is the architecture of record for the v1 rewrite.
> Work is tracked as tickets in [`tickets/`](tickets/) — see [`README.md`](README.md).

## Context

LLM-Middleman is a HACS custom integration (domain `llm_middleman`) that plugs into HA
Assist/Voice as a streaming `ConversationEntity`. The v0 build forwards turns to a single
bespoke `/v1/converse` SSE endpoint. The owner has decided to **re-architect from scratch**
around **backend presets**: pick a backend type in the config UI (OpenAI-compatible,
LangGraph, …), enter URL + auth, done. All research below was verified against live
2026 docs and HA core dev source by six Opus research agents this session.

**User decisions (locked):**
- Re-architect from scratch; v0 code is reference material.
- v1 presets: **OpenAI-compatible**, **LangGraph**, **Custom `/v1/converse` SSE** (v0
  contract survives as one preset), **Ollama native**, **n8n** (first-class).
- Every connector exposes its full useful configuration surface (per-connector matrix
  below) and streams asynchronously wherever the protocol allows (all five do; n8n also
  needs a blocking fallback because its streaming silently degrades when the workflow
  isn't stream-enabled).
- **Optional HA tool loop**: per-agent `CONF_LLM_HASS_API` for backends whose protocol
  carries tool calls (OpenAI-compatible, Ollama). LangGraph/converse/n8n stay text-only
  passthrough (their tools live server-side).
- **Parent entry + subentries** config model (core's 2025.8+ pattern).
- Live HA instance available for E2E verification.

## Verified constraints the design is built on

- HA 2026.7 / Python ≥3.14. Entity shape: `ConversationEntity` +
  `_async_handle_message(user_input, chat_log)`; stream via
  `chat_log.async_add_delta_content_stream(entity_id, generator)`;
  `_attr_supports_streaming = True` → ~0.5 s time-to-first-audio with streaming TTS.
  First delta of each block MUST carry `role`. `async_provide_llm_data` (not the
  deprecated `async_update_llm_data`).
- Subentries are a core config-entries feature since 2025.3, viable for custom
  integrations (openai_conversation migrated 2025.7). Template: core
  `ollama/config_flow.py` (simplest) and `openai_conversation/config_flow.py`
  (multi-type). Use post-2025.4 names (`_get_entry`, `_entry_id`).
- Stateless backends: replay history from `chat_log.content` each turn — core openai
  `_convert_content_to_param` (full replay) / ollama `_convert_content` + `_trim_history`
  (system + last `2*max_messages+1`). Tool loop: entity loops ≤ `MAX_TOOL_ITERATIONS=10`
  while `chat_log.unresponded_tool_results`.
- Backend axes to abstract: **stateless-replay vs stateful-thread** and **SSE vs NDJSON**.
  Auth shapes differ (`Authorization: Bearer` / `x-api-key` / none).
- Voice budget: time-to-first-token dominates; pipeline hard ceiling 300 s; stream every
  delta immediately, never buffer.
- v0 defects to design out (audit): per-line SSE parsing (spec requires buffering `data:`
  lines, dispatch on blank line); `ValueError`/`UnicodeDecodeError` escape the except and
  break the never-hangs guarantee; single 60 s total timeout kills slow-but-streaming
  backends; URL-as-unique_id blocks two agents on one backend; base-URL reachability probe
  validates nothing; no OptionsFlow; pyright config "standard" while docs claim strict;
  CI never runs typecheck.

## Architecture

### Package layout

```
custom_components/llm_middleman/
  __init__.py        # setup: build adapter → entry.runtime_data; forward CONVERSATION;
                     # async_migrate_entry (v0 entry → parent + one converse subentry)
  const.py           # DOMAIN, CONF_* keys, BACKEND_* type constants, defaults
  config_flow.py     # parent flow (backend-type menu → per-backend connection step,
                     # validated by adapter probe) + ConversationSubentryFlowHandler
  conversation.py    # ONE backend-agnostic entity: tool loop + guard + chat_log wiring
  backends/
    __init__.py      # BACKEND_TO_CLS: dict[str, type[BackendAdapter]] factory
    base.py          # BackendAdapter ABC, shared types
    openai_compat.py # /v1/chat/completions SSE; stateless replay; HA tools capable
    ollama.py        # /api/chat NDJSON; stateless replay + trim; HA tools capable
    langgraph.py     # /threads + /threads/{id}/runs/stream, stream_mode=messages-tuple;
                     # stateful (conversation_id → thread_id map); text-only
    converse.py      # v0 /v1/converse SSE contract; stateful (backend keys on
                     # conversation_id); text-only
    n8n.py           # Chat Trigger webhook; stateful (sessionId = conversation_id);
                     # streaming when workflow supports it + blocking fallback; text-only
    _sse.py          # spec-compliant SSE reader (shared)
  manifest.json / strings.json / translations/en.json / brand/
tests/               # conftest MockChatLog harness (carried from v0) + per-backend fakes
```

### Adapter interface (`backends/base.py`)

```python
@dataclass
class TurnContext:
    """Per-turn channel between entity and adapter (created fresh each turn —
    the adapter instance is shared across subentries/turns, so per-turn state
    must never live on the adapter itself)."""
    options: Mapping[str, Any]          # the agent subentry's options
    memory_key: str                     # session key derived by the entity (memory_scope)
    continue_conversation: bool = False # adapter may set; entity ORs into the result


class BackendAdapter(ABC):
    backend_type: ClassVar[str]
    supports_ha_tools: ClassVar[bool] = False       # gates CONF_LLM_HASS_API in subentry flow
    supports_memory_scope: ClassVar[bool] = False   # gates CONF_MEMORY_SCOPE in subentry flow

    def __init__(self, hass, session, connection_data) -> None:
        """Constructor contract: adapters read connection state (base_url, auth, …)
        from self; built once in __init__.py setup and stored in entry.runtime_data."""

    @classmethod
    @abstractmethod
    async def async_validate_connection(cls, hass, data) -> None:
        """Config-flow probe against the backend's REAL endpoint; raises on failure.
        openai: GET /v1/models · ollama: GET /api/tags · langgraph: GET /ok (fallback
        POST /assistants/search) · converse: transport-level check."""

    @classmethod
    async def async_list_models(cls, hass, data) -> list[str] | None:
        """Model catalog for the subentry dropdown; None = backend has no catalog."""
        return None

    @abstractmethod
    def stream_turn(
        self, chat_log, user_input, ctx: TurnContext,
    ) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        """One provider round-trip → canonical HA delta dicts.
        Stateless adapters rebuild provider messages from chat_log.content (with
        ollama-style trim via CONF_MAX_HISTORY) and pass HA tool schemas when
        chat_log.llm_api is set. Stateful adapters send only the new turn keyed by
        ctx.memory_key (e.g. mapped to a LangGraph thread_id)."""
```

The adapter instance lives in `entry.runtime_data` (with a shared
`async_create_clientsession`), built by `__init__.py::async_setup_entry` via the
`BACKEND_TO_CLS` factory. LangGraph's `memory_key → thread_id` map: in-memory for
`conversation` scope (HA rotates conversation_id after its 5-min session TTL — a new ID
simply creates a new thread); persisted via `helpers.storage.Store` for `device`/`agent`
scopes (see §Conversation continuity).

### Conversation entity (backend-agnostic, `conversation.py`)

`_async_handle_message`:
1. `async_provide_llm_data(user_input.as_llm_context(DOMAIN), llm_api_or_None,
   system_prompt, extra_system_prompt)`; `ConverseError` → `err.as_conversation_result()`.
   `llm_api` comes from the subentry's `CONF_LLM_HASS_API` — stored as a **list**,
   multi-select over `llm.async_get_apis()` (only offered when
   `adapter.supports_ha_tools`). The list form is free
   (`chat_log.async_provide_llm_data` accepts `str | list[str]`) and automatically
   includes tools from HA's MCP-client integration (each MCP entry registers its own
   `llm.API`) — users get MCP-server tools in the loop with zero code. Set
   `ConversationEntityFeature.CONTROL` iff an LLM API is configured (one line; makes the
   pipeline's local-intent fallback not steal device commands — exact core-openai
   pattern). Forwarding `extra_system_prompt` here is also the entirety of supporting
   `assist_satellite.start_conversation` (agent-initiated conversations work for free).
2. Tool loop (core openai `entity.py` shape): `for _ in range(MAX_TOOL_ITERATIONS)`:
   drive `chat_log.async_add_delta_content_stream(self.entity_id,
   _guarded(adapter.stream_turn(...)))`; break when
   `not chat_log.unresponded_tool_results`. Text-only adapters do at most one iteration.
3. `conversation.async_get_result_from_chat_log(user_input, chat_log)`.

**Never-hangs guard** (v0's best idea, hardened): `_guarded()` wraps every adapter stream —
catches `Exception` broadly (not just `ClientError`/`TimeoutError`; the v0 holes were
`ValueError` from the 64 KB readline limit and `UnicodeDecodeError`), logs with
`_LOGGER.exception`, and guarantees ≥1 `AssistantContent` (role-first delta, then a
fallback message) on every exit path. Empty-string deltas pass through untrimmed.

**Timeouts:** `aiohttp.ClientTimeout(total=CONF_TIMEOUT, sock_read=IDLE_TIMEOUT)` —
per-agent configurable total (default 60 s, range 10–300, webhook-conversation precedent)
plus a sock_read idle timeout (~30 s) so a responsive-but-slow stream isn't killed.

### Streaming parsers

- `_sse.py`: spec-compliant reader over `response.content` — accumulates consecutive
  `data:` lines, dispatches on blank line, handles CRLF and `:` comments,
  `decode(errors="replace")`, catches oversized-line `ValueError` and re-raises as a
  typed `BackendStreamError` the guard maps to fallback.
- Ollama NDJSON: line-per-JSON-object with `done: true` terminator (inside `ollama.py`).
- Delta extraction per preset: `choices[].delta.content` (openai-compat, `[DONE]`
  sentinel; tool_calls fragments accumulated by index) · `message.content` /
  `message.tool_calls` (ollama) · messages-tuple `content` filtered by
  `metadata.langgraph_node` (langgraph; terminal `event: end` / `event: error`) ·
  `text_delta`/`done`/`error` events (converse, per docs/knowledge/03 §4).

### Config flow

- **Parent flow:** step 1 = backend type (`SelectSelector` dropdown over `BACKEND_TO_CLS`
  keys — home-llm pattern); step 2 = per-backend connection form (URL normalized: strip
  trailing slash; auth field — Bearer token, x-api-key, or none per backend; LangGraph
  adds `assistant_id`, default `"agent"`), validated via
  `adapter.async_validate_connection`. **No URL-based unique_id** (v0 defect — duplicates
  are legitimate). Reconfigure step re-runs the same validation.
- **Subentry flow** (`async_get_supported_subentry_types` → `{"conversation": …}`,
  ollama template with `async_step_user = async_step_reconfigure = shared set_options`):
  agent name, system prompt (`TemplateSelector`), `CONF_LLM_HASS_API` (multi-select over
  `llm.async_get_apis()`, only when `supports_ha_tools`), model (openai-compat/ollama — populated from
  the probe's model list where possible), `CONF_MAX_HISTORY`, `CONF_TIMEOUT`.
- **Migration:** config-entry `VERSION = 2` + `async_migrate_entry` converting a v0 entry
  (url/token/system_prompt) into a converse-type parent + one conversation subentry, so
  the owner's installed v0 upgrades cleanly.

### Per-connector configuration matrix

Every option below is a real setting of the target protocol (verified against its current
docs / core-integration source this session). Parent = connection fields on the config
entry; Agent = per-agent subentry options. All connectors stream async via aiohttp.

**OpenAI-compatible** — streaming: SSE (`stream: true`), `[DONE]` sentinel.
- Parent: `base_url` (trailing slash stripped), `api_key` (optional; hint that many
  "compatible" servers require a non-empty dummy value).
- Agent: `model` (dropdown populated from `GET /v1/models`, free-text fallback),
  system prompt, `llm_hass_api`, `max_history`, `temperature`, `top_p`, `max_tokens`
  (the core-openai option set), `timeout`.

**LangGraph** — streaming: SSE, `stream_mode=messages-tuple`; terminal `end`/`error`.
- Parent: `base_url` (deployment URL; same wire API for `langgraph dev` / self-hosted /
  cloud), `api_key` (optional, sent as `x-api-key`), `assistant_id` (graph name or UUID,
  default `"agent"`).
- Agent: system prompt (prepended as a system message when the graph uses MessagesState;
  optional), `input_messages_key` (default `messages` — graphs define their own input
  schema), `response_node_filter` (optional `langgraph_node` name so intermediate/tool
  node chatter isn't spoken), `timeout`. Threaded runs by default (server-side state);
  `stateless_runs` toggle (`POST /runs/stream`) for graphs that manage nothing.

**Ollama native** — streaming: NDJSON, `done: true` terminator.
- Parent: `base_url` (host root, not /v1), `api_key` (optional).
- Agent: `model` (from `GET /api/tags`), system prompt, `llm_hass_api`, `max_history`
  (trim), `num_ctx`, `keep_alive`, `think` (the core-ollama option set), `timeout`.

**Custom `/v1/converse`** — streaming: SSE `text_delta`/`done`/`error` (v0 contract).
- Parent: `base_url`, bearer `token`.
- Agent: system prompt (optional; note the backend owns its own prompt — this one rides
  the request only if we extend the contract), `timeout`. Backend keys its own state on
  the forwarded session key; `done.continue_conversation` is honored via the explicit
  ConversationResult override (see follow-up-listening section).

**n8n (Chat Trigger / plain Webhook)** — streaming: **NDJSON** `StructuredChunk` lines
(`{"type":"begin"|"item"|"end"|"error","content":…}`, shipped n8n 1.103.0), NOT SSE.
Accumulate `item.content`; **EOF is the true done signal** (multiple begin/end cycles per
run — never treat `end` as terminal); `error` chunks and HTML bodies (proxy timeout) →
fallback. Non-streaming mode returns one JSON object. **Branch on actual response
content-type/first-bytes, not the config toggle** — n8n silently sends a blocking body
when the workflow isn't stream-enabled on BOTH the Chat Trigger and the AI Agent node.
- Parent: `webhook_url` (opaque full production chat URL, `/webhook/<id>/chat` — user
  pastes it verbatim), `target_type` (Chat Trigger | plain Webhook+Respond-to-Webhook —
  gates the `action` field), auth type + credentials (none / basic / custom header —
  Chat Trigger natively offers none+basic; header covers plain-webhook Header Auth and
  reverse proxies).
- Agent: `streaming` toggle (default OFF — help text explains the both-nodes requirement),
  `input_field` (default `chatInput`; the node's `chatInputKey` is configurable),
  `output_field` (default `output`, fallback `text`), `session_field` (default
  `sessionId`), optional system prompt (extra body field for the workflow to use),
  `timeout` (default 30 s).
- Request: `POST {url}` with `{"action":"sendMessage", "<session_field>":
  conversation_id, "<input_field>": text}` (`action` omitted for plain Webhook).
- Session: HA `conversation_id` → `sessionId` (n8n memory nodes partition on it).
- Misconfig visibility: missing output field in a blocking reply → surface an error
  (mirrors n8n's "wrong key returns whole object"), never silently speak JSON.

### Follow-up listening (agent clarifying questions)

Verified against the installed HA source this session: `ChatLog.continue_conversation`
(`conversation/chat_log.py:355-373`) is a computed property — true when the last
assistant message ends with `?` / `;` (Greek) / `？` — and
`async_get_result_from_chat_log` copies it into
`ConversationResult.continue_conversation` (`conversation/util.py:45`,
`models.py:80`), which voice satellites use to keep the mic open for a wake-word-free
follow-up. Design:
- **Automatic (all connectors):** any reply ending in a question mark keeps HA listening.
  Works for free; document it per connector.
- **Explicit override:** the entity builds its `ConversationResult` from
  `async_get_result_from_chat_log` and ORs in an adapter-provided explicit flag —
  converse preset honors `done.continue_conversation` (finally wiring the documented
  contract field v0 ignored); n8n blocking replies may carry an optional
  `continueConversation` boolean output field. OpenAI-compat/Ollama/LangGraph have no
  protocol slot → automatic detection only.
- The follow-up turn arrives with the SAME HA `conversation_id` (5-min session TTL,
  `helpers/chat_session.py:28`), so backend context holds across the clarify→answer loop
  on every connector.

### Conversation continuity (thread identity)

HA already provides the per-session key: `conversation_id` (ULID, rotates after the
5-minute chat-session TTL). The adapter layer builds on it with a per-agent
**`memory_scope`** option controlling the session key sent to/mapped for the backend:
- `conversation` (default) — key = HA `conversation_id`; continuity for the session,
  fresh thread after the TTL. Matches Assist semantics.
- `device` — key = stable per `user_input.device_id` (falls back to conversation scope
  when no device): each satellite/room keeps one long-lived backend thread across days.
- `agent` — one global key per agent subentry: a single continuous relationship.

Per connector: LangGraph maps key → `thread_id` (mapping persisted via
`helpers.storage.Store` so device/agent threads survive HA restarts; conversation-scoped
mappings stay in-memory); n8n sends the key as `sessionId` (its memory nodes partition on
it); converse sends it as `conversation_id` (backend owns state). **Honest limit:**
stateless backends (OpenAI-compat, Ollama) derive context from replaying HA's ChatLog,
which only spans the HA session — for them `device`/`agent` scope cannot resurrect
history HA discarded, so the option is hidden (adapter capability flag). Shim-side
history persistence for stateless backends is explicitly out of v1 (would fatten the
shim; revisit as v2 if wanted).

### Serialization & safety details

- Any HA object serialized into a request body uses `json.dumps(..., default=str)`
  (webhook-conversation issue #40 class of crash).
- Bearer/x-api-key values redacted from all logs. Untrusted stream content is data, never
  parsed as instructions.
- Tool-call args from small local models may be malformed — copy core ollama's
  `_parse_tool_args` repair (drop None/empty).

### Adjacent HA AI capabilities — explicit in/out decisions

Full sweep of HA 2026.7.1's AI/LLM surface performed against the installed source
(file:line evidence in session research). Decisions:

**In v1 (cheap, already folded into the sections above):**
- Streaming deltas + `supports_streaming` (only path to token-level TTS).
- `CONF_LLM_HASS_API` as a list (MCP tools free) + `ConversationEntityFeature.CONTROL`.
- `extra_system_prompt` + stable `conversation_id` (= `assist_satellite.start_conversation`
  support; `announce`/`ask_question` never invoke the agent — nothing to do).
- `continue_conversation` derived + explicit `ConversationResult` override.
- README/docs note: sentence triggers and `prefer_local_intents` intercept turns BEFORE
  any agent — preempts "my agent never got the message" reports.
- `diagnostics.py` (anthropic-style: redact API keys, prompts, URLs) in Phase 4 — tiny,
  HACS-quality win.

**Fast-follow (designed-for, not built in v1):**
- **AI Task `GENERATE_DATA`** as an `ai_task_data` subentry type: reuses the same ChatLog
  forwarding path (`AITaskEntity._async_generate_data` gets a primed ChatLog); honest
  `structure` support only where the backend has native structured output (OpenAI-compat
  `response_format`, Ollama `format`); prompt-injected best-effort (documented) for
  converse/n8n/LangGraph. This is the non-voice path the original docs envisioned.
- **Token stats** via `chat_log.async_trace({"stats": {input_tokens, output_tokens}})` —
  lights up the 2025.12/2026.4 AI debug views when backends report usage.
- **External tool-activity surfacing**: converse's documented `tool_activity` event (and
  LangGraph tool chunks) → `ToolInput(external=True)` +
  `async_add_assistant_content_without_tools`, so the brain's own tool calls appear in
  Assist's "Show details" view without HA executing anything (wyoming is prior art).

**Skip (with reasons, so it isn't re-litigated):**
- AI Task attachments / `GENERATE_IMAGE`, STT/TTS platforms — contradict text-only scope;
  attachments never appear on the conversation path anyway.
- Registering our own `llm.API` — our tools live in the external backends.
- Token-usage sensors — no core prior art; trace stats is the blessed channel.
- Repairs flows — nothing to deprecate yet.

### Housekeeping in scope

- `manifest.json`: drop `requirements: ["aiohttp"]`; revisit `iot_class`
  (`local_polling`); bump `hacs.json` `homeassistant` min to the subentry-safe floor
  (≥2025.8 recommended); verify `codeowners` is a real GitHub handle.
- `pyproject.toml`: ruff `target-version` → py314 floor to match HA 2026.x; align
  pyrightconfig (`strict` vs current `standard`) with the advertised gate; **add
  `just typecheck` to CI** (lint.yml currently never runs it).
- Docs rewrite to match the new architecture: `docs/knowledge/03` (shim doc) and
  `docs/external-agent-handoff/` updated — `/v1/converse` is no longer THE contract, it's
  one preset; fix the stale claims (context/continue_conversation "wired"); quarantine or
  trim `docs/knowledge/01` (verbatim sibling-repo content describing code that doesn't
  exist here); update CHANGELOG.

## Implementation phases (each = branch → PR, gates green; never commit to main)

1. **Foundation + OpenAI-compatible.** New package layout, `BackendAdapter` ABC +
   factory, `_sse.py`, guarded backend-agnostic entity, parent+subentry config flow,
   v0→v1 entry migration, OpenAI-compatible adapter (text-only first). Port the
   MockChatLog conftest; per-adapter fake-stream test harness. Typecheck in CI.
2. **Remaining adapters.** converse (port v0 logic through the new parser/guard), Ollama
   NDJSON, LangGraph (thread mapping + node filtering), n8n (dual streaming/blocking
   modes). Each with fallback-path tests (done-with-no-delta, silent stream end,
   error-after-deltas, oversized line, malformed JSON — the v0 untested surface); n8n
   additionally tested for the blocking-response path and the wrong-mode mismatch.
3. **HA tool loop.** `CONF_LLM_HASS_API` (list, multi-select) in the subentry flow
   (capability-gated), `ConversationEntityFeature.CONTROL`, tool-schema formatting +
   tool_call delta accumulation for openai-compat/ollama, bounded iteration. Core
   `openai_conversation/entity.py` is the line-by-line template.
4. **Polish & release.** Docs rewrite, `diagnostics.py`, manifest/hacs bumps, hassfest
   green, brands check, live E2E, HACS release. Fast-follow items (AI Task data, trace
   stats, external tool-activity surfacing) stay on the roadmap, not in v1.

## Verification

- Per phase: `just check` (lock + lint + fmt + tests) + `just typecheck` + hassfest
  (validate.yml). Baseline recorded before each phase; whole-gate reruns after.
- Adapter unit tests drive fake aiohttp streams through the REAL parser (raw bytes with
  split chunks/CRLF/multi-line data — not pre-split lines like v0's `_FakeContent`).
- Live E2E on the owner's HA instance: install via HACS custom repo; OpenAI-compatible
  preset against the owner's local llama.cpp/OpenAI-compatible proxy; LangGraph preset
  against a `langgraph dev` sample graph; converse preset against a minimal SSE stub (in
  /tmp, deleted after). Voice latency check on real hardware is owner-run.

## Implementation-time checkpoints (flagged, not blockers)

- Confirm against pinned HA source at build time: exact delta-stream tool-execution flow
  (`async_add_delta_content_stream` + `unresponded_tool_results` loop) and whether
  `AbstractConversationAgent` base is still needed alongside `ConversationEntity`.
  (`continue_conversation` mechanics already verified against the installed HA — see the
  follow-up-listening section.)
- LangGraph `messages-tuple` frame shape and terminal event names: verify against a live
  `langgraph dev` capture before finalizing that adapter's parser (researcher flagged
  this as their least-confident item).
- AG-UI / Dify / Anthropic / Responses-toggle: explicitly out of v1; the adapter
  interface was shaped so each lands as one new module later.
