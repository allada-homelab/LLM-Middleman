# 01 — Home Assistant 2026.7 Reference (Conversation · LLM · Voice · AI Task · MCP)

> **What this is.** The full synthesized research report from a 12-agent study of
> Home Assistant 2026.7 (developer docs + `home-assistant/core` source + the official
> `ollama` / `openai_conversation` / `anthropic` / `google_generative_ai` integrations).
> It was originally written to guide a rewrite of the sibling `LLM-Home-Controller`
> integration, and is **retained here verbatim** because it is the authoritative HA
> knowledge base for anything built on the Assist / Conversation / LLM stack — the
> passthrough shim included.
>
> **How to read it for the shim project:**
> - **§1–§6 and §10** = pure HA best-practice / API / LLM / voice / AI-Task / MCP /
>   testing / distribution knowledge. Directly applicable. Read these first.
> - **§7–§9** review the *prior* `LLM-Home-Controller` implementation (an agent that
>   embeds the LLM *inside* HA). Kept as **lessons learned**. The passthrough shim is
>   architecturally different — it *forwards* the turn to an external service instead
>   of running the model in-process — **but the HA plumbing it touches
>   (ConversationEntity, ChatLog, streaming deltas, Assist exposure, conversation_id)
>   is identical**, so every API fact here still applies to the shim.
>
> **Cross-refs:** `03-the-shim.md` maps these primitives onto the passthrough shim;
> `05-architecture-decisions-and-tradeoffs.md` records why we chose passthrough.

---

# LLM Home Controller — Rewrite Research Report (HA 2026.7)

*Principal-engineer synthesis of current Home Assistant best practice + a review of the existing repo, to direct a from-scratch rewrite of an agentic, multi-tool-call LLM home controller backed by OpenAI-compatible and Anthropic endpoints.*

---

## 1. Executive summary

The current (2026.7) best-practice shape of an agentic LLM conversation integration is now well-defined and **the existing repo already sits on top of it** — the rewrite is a consolidation and correctness pass, not a paradigm change. Verified against HA core's own `ollama`, `openai_conversation`, `anthropic`, and `google_generative_ai_conversation` integrations, the blessed skeleton is:

- One **parent `ConfigEntry`** per backend connection (URL/key/type), holding a live client in **`entry.runtime_data`** (typed alias `type LLMHomeControllerConfigEntry = ConfigEntry[aiohttp.ClientSession]`).
- **Config subentries** (`subentry_type` in `{"conversation","ai_task_data"}`) for per-agent config, each with its own `ConfigSubentryFlow` reconfigure step — **no separate OptionsFlow**.
- A shared **`BaseLLMEntity(Entity)`** mixin carrying one `_async_handle_chat_log()` tool-loop, reused by a triple-inheritance `ConversationEntity + AbstractConversationAgent + BaseLLMEntity` and (optionally) an `ai_task.AITaskEntity + BaseLLMEntity`.
- **`ChatLog` owns tool execution.** The integration writes only two per-provider functions — a batch `_convert_content()` (ChatLog → wire messages) and a streaming `transform_stream()` (SSE → `AssistantContentDeltaDict`) — and drives `chat_log.async_add_delta_content_stream()`, which auto-executes non-`external` tools and appends `ToolResultContent`. Loop while `chat_log.unresponded_tool_results` is truthy, bounded by `MAX_TOOL_ITERATIONS`.

**The 5–8 highest-leverage decisions for the rewrite:**

1. **Keep the provider `Protocol`/adapter seam and the entry/subentry + runtime_data architecture verbatim.** Both are confirmed-current and are the repo's strongest assets.
2. **Fix the custom-tool exposure gap (highest-value correctness/design change).** Today custom tools (memory, entity-query, user service tools) are injected onto `chat_log.llm_api.tools` and silently vanish when the user selects no HASS API. Decouple by registering a **dedicated `llm.API`** for this integration's own tools, composable with or without Assist.
3. **Stop string-splicing HA core's AssistAPI system prompt** for custom entity context; own the prompt via a custom `llm.API` extension point instead.
4. **Fix the "memory" naming/persistence lie.** `CONF_MEMORY_ENABLED` controls a rolling in-memory buffer that is never persisted and dies on every restart, despite `ConversationEntity` already extending `RestoreEntity`. Either persist it (Store-backed, like `AgentMemoryStore`) or rename it.
5. **Make blocking file I/O async** (`att.path.read_bytes()` → `hass.async_add_executor_job`) — a concrete violation of HA's event-loop contract flagged by the blocking-call detector.
6. **Adopt Ollama's small-model hardening** (`_fix_invalid_arguments`, `_parse_tool_args`, `_trim_history`) — directly targeting the open-weight-model failure classes this repo's own commit log ("tool crash without LLM API", "memory replay loss") has already hit.
7. **Split `MAX_TOOL_ITERATIONS` per call site** (conversation ≈ 10 for a waiting human; AI Task ≈ 1000 for autonomous background jobs), and treat streaming (`_attr_supports_streaming = True`, emitting text early) as a **hard requirement** for acceptable agentic-voice latency.
8. **Formalize distribution/quality**: add `integration_type`, `codeowners`, a seeded `quality_scale.yaml` (Bronze floor, Silver reachable), and reconcile the `hacs.json` HA floor (`2025.1.0`) with the APIs actually used (`ConfigSubentry`, `ai_task`, `thinking_content`).

**One fork worth resolving up front:** OpenAI *Responses API* vs *Chat Completions*. The rewrite targets OpenAI-*compatible* backends (llama-swap/vLLM/LiteLLM/Ollama), which speak Chat Completions — so use **`ollama/entity.py` as the structural template**, `anthropic/entity.py` for the Claude leg, and only borrow reasoning/structured-output *gating patterns* (not message plumbing) from `openai_conversation`.

---

## 2. HA integration fundamentals for a rewrite

### Structure & manifest
Required `manifest.json` keys: `domain`, `name`, `codeowners`, `integration_type`, `iot_class`. For a **custom/HACS** integration `version` is **required** (inverted vs core, which must omit it). The existing manifest has `version: "0.1.0"` (correct) but **empty `codeowners`** and **no `integration_type`** (defaults silently to `hub`; dev docs recommend setting it explicitly — `service` fits a single-backend connection). `iot_class` is currently `local_polling`; a request/response conversation agent is arguably `local_push` or `calculated`, but this is low-stakes.

### Config entries / subentries vs options
The subentry pattern (parent = connection, children = per-agent) is the current recommended shape for "one connection → many children" and matches `openai_conversation` exactly. Declared via:

```python
@classmethod
@callback
def async_get_supported_subentry_types(cls, config_entry) -> dict[str, type[ConfigSubentryFlow]]
```

Each subentry gets its own `ConfigSubentryFlow` (`async_step_user`, `async_step_reconfigure`, `async_step_init`) — **this replaces OptionsFlow**. Never mutate `entry.data`/`entry.options` in place; always `hass.config_entries.async_update_entry(...)`.

**Pros of subentries (recommended, keep):** clean UI grouping of N agents under one connection; per-agent reconfigure; no duplicated API URL/key; upstream precedent. **Cons:** thinner prose docs (mechanics come from reading source), more flow classes. **Alternative rejected:** one parent entry per agent — simpler but duplicates connection config and breaks the shared-session model.

### runtime_data & platforms
`entry.runtime_data = aiohttp.ClientSession(...)` is the current idiom (vs `hass.data[DOMAIN]`). Forward platforms with `await hass.config_entries.async_forward_entry_setups(entry, ["conversation", "ai_task", "sensor"])`. Register listener cleanup via `entry.async_on_unload(...)` / `async_on_unload(...)`.

### DataUpdateCoordinator — *do not add*
A conversation agent is stateless per-turn; there is no shared polled state to coordinate. Anthropic core uses a coordinator **only** because its API exposes `client.models.list()` capability discovery. OpenAI-compatible backends generally don't, so a coordinator would be unearned complexity (and contradicts this project's "simplicity first" guideline). Keep the plain client in `runtime_data`.

### Quality scale
`quality_scale.yaml` is now a machine-checked artifact: flat `rules:` map, each `done` or `{status: exempt, comment: ...}`. Bronze = floor (config flow, tests, docs, `runtime-data`, `entity-unique-id`, `has-entity-name`); Silver adds `action-exceptions`, `config-entry-unloading`, `reauthentication-flow`, `test-coverage`; Gold adds translations/reconfigure/repair; Platinum wants `strict-typing` + `inject-websession`. For a conversation-only integration, reuse `openai_conversation`'s exemption language for device/discovery Gold rules ("Service integration, no discovery.").

### HACS distribution
One integration per repo under `custom_components/<domain>/`; `hacs.json` needs at least `name`. **Brand icons: use the local `brand/` folder** (HA 2026.3+) — the existing repo already ships `brand/{icon,logo}{,@2x}.png` (good; the `home-assistant/brands` repo no longer accepts custom-integration PRs). GitHub Releases give the version-picker UI (HACS falls back to default branch otherwise).

---

## 3. The Conversation/Assist + LLM tool-calling stack

### The entity contract
```python
class LLMHomeControllerConversationEntity(
    conversation.ConversationEntity,       # extends RestoreEntity; @final internal_async_process
    conversation.AbstractConversationAgent,  # ABC required by async_set_agent's type hint
    LLMHomeControllerBaseLLMEntity,        # plain Entity subclass, shared tool loop
): ...
```
- Override **only** `_async_handle_message(self, user_input, chat_log) -> ConversationResult`. Never touch `async_process` (retired override point) or `internal_async_process` (`@final`). Confirmed against `conversation/entity.py`.
- `AbstractConversationAgent` is non-optional in practice: `conversation.async_set_agent(hass, config_entry, agent)` type-hints `agent: AbstractConversationAgent`, and the registry is keyed by `config_entry.entry_id`.
- Register in `async_added_to_hass`; unregister via `async_unset_agent`.

### The turn chain
`_async_handle_message` does exactly three calls, wrapping only the first in `try/except conversation.ConverseError`:
1. `chat_log.async_provide_llm_data(llm_context, user_llm_hass_api=..., user_llm_prompt=..., user_extra_system_prompt=...)` — **use this, never `async_update_llm_data`**, which now raises (`frame.report_usage(breaks_in_ha_version="2026.1")`, and current is 2026.7). This resolves `chat_log.llm_api` from `CONF_LLM_HASS_API` (imported from `homeassistant.const`) and sets `chat_log.content[0] = SystemContent(...)`.
2. `self._async_handle_chat_log(chat_log)` — the tool loop.
3. `conversation.async_get_result_from_chat_log(user_input, chat_log)` — builds `ConversationResult`; raises `HomeAssistantError` if last content isn't `AssistantContent`.

### The tool loop (the one primitive to get exactly right)
```python
for _iteration in range(max_iterations):        # MAX_TOOL_ITERATIONS = 10
    messages = convert_content(chat_log.content)
    payload  = build_payload(messages, tools, ...)   # stream=True
    async for _ in chat_log.async_add_delta_content_stream(agent_id, transform_stream(resp)):
        pass
    if not chat_log.unresponded_tool_results:    # == (content[-1].role == "tool_result")
        break
```
`async_add_delta_content_stream` accumulates `content`/`thinking_content` by concatenation, extends `tool_calls`, and the moment a non-`external` `llm.ToolInput` delta completes it schedules `hass.async_create_task(self.llm_api.async_call_tool(tool_call))` — **tool execution runs concurrently with continued streaming**, then results are appended as `ToolResultContent` (with a `ConversationTraceEventType.TOOL_CALL` trace for free). Delta vocabulary is exactly `{role, content, thinking_content, tool_calls, native}` (assistant) and `{role:"tool_result", tool_call_id, tool_name, tool_result}` (external). `native` carries non-JSON-serializable provider state and is deliberately withheld from delta listeners.

- **`continue_conversation`** is a computed property: last message is assistant, non-null, ends in `?`/`？`/`;`. Don't build custom keep-listening logic.
- **Session/conversation_id** lifecycle (fresh ULID unless a non-ULID custom id supplied; 5-min `CONVERSATION_TIMEOUT`) lives in `homeassistant.helpers.chat_session` — nothing to implement.
- `ConverseError` is for **pre-flight failures only** ("Will not be stored in the history"); mid-loop tool errors should surface as `ToolResultContent`/`AssistantContent` so the failed turn stays visible.

### `llm.Tool` + AssistAPI + custom tool exposure
- `AssistAPI` (`llm.LLM_API_ASSIST = "assist"`) turns each non-ignored HA intent into an `IntentTool` (this is how `HassTurnOn`/`HassLightSet`/etc. reach the LLM), plus `GetLiveContextTool`, `GetDateTimeTool`, `ScriptTool`, `CalendarGetEventsTool`, `TodoGetItemsTool` — all gated by **Assist entity exposure**. Nothing exposed → `NO_ENTITIES_PROMPT` and no device tools (a common "the LLM has no tools" support issue that is config, not a bug).
- **Two ways to add custom tools:** (a) inject into `chat_log.llm_api.tools` after `async_provide_llm_data` (simple, reuses AssistAPI's prompt + `custom_serializer`, but is a **no-op when `chat_log.llm_api is None`** — the repo's current gap); (b) register a standalone `llm.API` via `llm.async_register_api(hass, MyAPI(...)); entry.async_on_unload(unreg)`, independently selectable in the `CONF_LLM_HASS_API` multi-select and mergeable with Assist. **Recommendation: register a dedicated `llm.API`** so this integration's tools work regardless of Assist selection.
- Always convert `Tool.parameters` via `voluptuous_openapi.convert(schema, custom_serializer=chat_log.llm_api.custom_serializer)` — pass the **same** serializer AssistAPI uses (`selector_serializer`) or `selector.Selector` params fail to round-trip.

### Structured output
Not a first-class `llm` helper primitive — each integration converts its own `vol.Schema` and passes a provider-specific param. This is a per-provider concern (see §4/§5).

---

## 4. AI Task, Voice pipeline, and MCP

### AI Task (`ai_task`, introduced 2025.8)
One-shot, stateless generation for automations/scripts: services `ai_task.generate_data` / `ai_task.generate_image`. Reuses the same `ChatLog`/tool machinery but **always opens a fresh `ChatSession` (`conversation_id=None`)** — never resumes a conversation. `AITaskEntity` extends `RestoreEntity` directly; override `_async_generate_data`/`_async_generate_image` (never the `@final` `internal_*` or `state`). `AITaskEntityFeature` = `GENERATE_DATA=1 | SUPPORT_ATTACHMENTS=2 | GENERATE_IMAGE=4`.

Key facts for the rewrite:
- `GenDataTask.structure` is a **`vol.Schema` built from HA selectors, NOT raw JSON Schema** — run it through `voluptuous_openapi.convert(...)` then a per-backend strictness pass (OpenAI forces `additionalProperties:false` + all-required; local backends differ — verify per target).
- Structured output is fragile: after the call, check `isinstance(chat_log.content[-1], AssistantContent)` then `json_loads` its `.content`, raising `HomeAssistantError` on failure. Small local models (e.g. the linked `gemma-3-27b-it` bug) frequently emit non-conforming JSON even with a schema request.
- `max_iterations=1000` for AI Task vs ~10 for conversation.
- **Verdict: adopt.** The repo already ships `ai_task.py` (104 lines) reusing `_async_handle_chat_log` with a `structure` override — this is the right shape for autonomous, structured, multi-tool decisions ("evaluate whether to adjust the thermostat → `{action, reason}`") and is squarely on-theme for "agentic control." Keep it; give it its own usage sensor (currently untracked).

### Voice pipeline (`assist_pipeline`)
Stages: wake_word → stt → intent → tts. The **intent stage tries sentence triggers + local intents (`prefer_local_intents`) before falling back to the LLM agent** — timers and common device commands deliberately bypass the (slow, multi-tool) LLM path. **Do not try to own timer/simple-intent handling.**

The single most important latency lever: with `_attr_supports_streaming = True` **and** a streaming-capable TTS engine (`tts_stream.supports_streaming_input`), the pipeline's `chat_log_delta_listener` starts feeding TTS after `STREAM_RESPONSE_CHARS = 60` accumulated characters — the assistant can start speaking before the tool loop finishes. **Agentic-latency UX tradeoff:** a long multi-tool turn with no interim text = dead air on real hardware. Design the agent to emit *some* assistant text early (a preamble) rather than only a final summary after all tools resolve. Where the first request *requires* a tool before any text (e.g. "what's the temperature"), streaming can't help — that's an unresolved gap; there's no first-class "filler utterance" primitive as of 2026.7. Consider a lower per-call iteration cap for voice-originated turns (distinguishable via `ConversationInput.device_id`). `AssistSatelliteEntity` is a satellite/hardware concern — **not needed** in a conversation-agent integration.

### MCP — out of scope for the core loop
Two independent core integrations, both bottoming out on the same `llm.API`/`ChatLog` surface this integration already uses:
- **`mcp_server`** makes HA an MCP *server* (exposes selected `llm.API`s over `/api/mcp`, bearer-token auth). Orthogonal — a user installs it alongside your agent to reach your tools from Claude Desktop/ChatGPT; **zero code in your integration.**
- **`mcp`** makes HA an MCP *client* (wraps remote tools as `llm.Tool`s in a synthetic `llm.API`, Tools-only — no Prompts/Resources/Sampling). A user adds its API id to your subentry's `CONF_LLM_HASS_API` list; **zero code in your integration.**

**Verdict: neither consume nor expose MCP inside the rewrite.** Building either would reimplement stock integrations with no functional gain. Document them as purely-additive companion options.

---

## 5. Reference integrations — the blessed pattern

All four official integrations share one skeleton (§1). They diverge only on wire API:

| Integration | Wire API | Best used as template for | Notable extras |
|---|---|---|---|
| **`ollama`** | flat message list + `tools` + `stream=True` (Chat-Completions-shaped) | **the OpenAI-compatible leg** (llama-swap/vLLM/LiteLLM) | `_fix_invalid_arguments` / `_parse_tool_args` (small-model tool-arg repair), `_trim_history` (context-window trimming via `CONF_MAX_HISTORY`) |
| **`anthropic`** | Claude Messages API | **the Claude leg** | extended thinking + `native` signature round-tripping, prompt caching, native `json_schema` output_config with forced-tool fallback; uses a `DataUpdateCoordinator` for capability discovery (Anthropic-specific — don't copy) |
| **`openai_conversation`** | **Responses API** (`client.responses.create`) | reasoning/structured-output *gating patterns only* (`model.startswith(("o","gpt-5"))`, `_adjust_schema` strict-mode) | **do NOT copy message plumbing** — targets the wrong endpoint for OpenAI-*compatible* backends |
| **`google_generative_ai`** | Gemini | reference for structured-output fragility | same `json_loads` pattern, same failure modes |

**Two things worth importing that the repo likely lacks:** (1) Ollama's small-model tool-arg repair + history trimming; (2) a two-tier structured-output strategy (native `json_schema` when supported, forced-tool-call fallback otherwise) — because local backends' structured-output support is heterogeneous and unadvertised.

**Shared invariants to treat as law:** system content is always `chat_log.content[0]` (`SystemContent`); tool execution and loop continuation belong to `ChatLog`, not the integration; `external=True` only for provider-side/server tools; reasoning continuity for Anthropic/OpenAI requires replaying `AssistantContent.native` (opaque signature/encrypted payload), not just `thinking_content` text.

---

## 6. Testing, quality scale, and distribution plan

**Testing (carry the existing suite's shape forward — it's a genuine strength at ~5.9k LOC):**
- **`MockChatLog`** — vendor a dataclass subclass of the *real* `conversation.ChatLog` in `tests/conftest.py`, patching only `async_call_tool` to serve canned per-`tool_input.id` results. Core moved this class into `tests/components/conversation/__init__.py` (PR #138112) and it isn't importable at runtime — re-diff against the current dev-branch shape before trusting a vendored copy.
- **SSE/stream tests** — mock at the SDK/client stream-create level, yielding scripted event sequences including error/incomplete/failed paths (mirror `openai_conversation/conftest.py`). The repo already unit-tests each `transform_stream`.
- **Gaps to close:** (a) one true end-to-end streaming test driving `async_add_delta_content_stream` against a real `MockChatLog` (not mocking `_async_handle_chat_log` out); (b) assert the iteration-cap produces a clean user-visible failure; (c) pick **one** test-construction idiom (real `MockConfigEntry`+`ConfigSubentry` for entity-lifecycle tests; `MagicMock(spec=...)` acceptable only for pure provider-adapter tests) and document it in `tests/CLAUDE.md`.
- Consider VCR-style cassettes against a real llama-swap/vLLM response and hypothesis-fuzzing the SSE parsers (they handle untrusted wire data).
- `enable_custom_integrations` fixture is mandatory; order it *after* fixtures like `recorder_mock`. Reconcile `pytest-asyncio` mode against the pinned version.

**Quality scale:** seed `quality_scale.yaml` from `openai_conversation`'s file now; target Bronze (config-flow tests, `test-before-configure/setup`, `runtime-data`, `entity-unique-id`) → Silver (`reauthentication-flow` on 401, `action-exceptions`, `config-entry-unloading`, `exception-translations`). Put `exceptions` keys in `strings.json` from day one (retrofitting means hunting every raise site). Structure `config_subentries` in `strings.json` mirroring `config`'s nested shape.

**Distribution:** keep the minimal single-job CI (`uv lock --check` → `uv sync --locked --dev` → ruff → format → pytest) plus the separate hassfest `validate.yml`; basedpyright/pre-commit stay local-only. **Reconcile `hacs.json`'s `2025.1.0` floor** against APIs actually used (`ConfigSubentry`, `ai_task`, `thinking_content`) — either raise the floor to match reality or CI-test against it. Fill `codeowners`. The `brand/` folder is already present and correct.

---

## 7. Review of the current repo

**What it is:** a working HA custom integration (~3.2k LOC source, ~5.9k LOC tests) registering a conversation agent (and AI Task entity) against OpenAI Chat-Completions, OpenAI Responses, or Anthropic Messages backends. Parent entry validates connectivity via `/models` and stores an `aiohttp.ClientSession` in `runtime_data` (`__init__.py:35-55`); `conversation`/`ai_task_data` subentries hold per-agent config. The turn is driven from `conversation.py:_async_handle_message` → `entity.py:_async_handle_chat_log` (`371-546`), a bounded 10-iteration loop that re-serializes `chat_log.content` per iteration, POSTs with retry + primary→fallback model chain, and streams into `async_add_delta_content_stream`. Providers sit behind a clean `LLMProvider` `Protocol` (`providers/__init__.py:13-69`) with three adapters.

**Strengths to keep:**
- **Provider `Protocol`/adapter split** — cleanly isolates three genuinely incompatible wire formats; the single most reusable asset.
- **Correct HA plumbing** — subentries, `runtime_data`, triple inheritance, `async_set_agent`/`unset`, `_async_handle_message` (never overriding `async_process`), `async_get_result_from_chat_log`, v1→v2 migration.
- **Real streaming** into `async_add_delta_content_stream` with thinking/reasoning + usage capture (load-bearing, not scaffolding — `stream:True` is the only transport).
- **Retry + sticky model-fallback** (`entity.py:242-369`) — distinguishes retryable (429/5xx/timeout) from terminal (400/401/404), honors `Retry-After`, never fails over on auth, keeps fallback sticky across tool iterations (with a dedicated test at `test_entity.py:1798`).
- **Unified structured-output routing** (static `CONF_RESPONSE_FORMAT`/`CONF_JSON_SCHEMA` + AI Task `structure` → one `extra_options["json_schema"]` via `voluptuous_openapi`, `entity.py:430-442`).
- **Explicit agent-invoked memory tools** (Save/Update/Remove) persisted via `Store` — a sound pattern (model decides what's durable).
- **Dispatcher-based usage sensor** with `RestoreEntity` `TOTAL_INCREASING` counters.
- **Test suite quality** — high coverage, systematic error paths, real `MockChatLog`, regression tests tied to actual bugfixes.

**Weaknesses / tech-debt / over-engineering / correctness risks:**
- **[Correctness/design] Custom-tool exposure gap** (`entity.py:402-417`, `conversation.py:254-259`): custom tools inject onto `chat_log.llm_api.tools` only `if chat_log.llm_api:` — so with no HASS API selected, memory + entity-query + user service tools **silently disappear** (debug-log only). No independent tool path.
- **[Correctness risk] System-prompt marker splicing** (`conversation.py:274-324`, hardcoded `"Static Context: An overview..."`): rewrites a substring of HA core's AssistAPI prompt; silently degrades to append the moment core reformats, with no user-visible signal.
- **[Correctness] "Memory" persistence lie** (`conversation.py:184-185`): `CONF_MEMORY_ENABLED`'s rolling history is a bare instance attribute, never Store-backed / never wired to `RestoreEntity`, so it dies on every restart/reload despite reading as durable. The `is_fresh` new-conversation heuristic (`len(chat_log.content) <= 2`, `conversation.py:209`) is fragile, and it may duplicate continuity `chat_log` already provides per `conversation_id`.
- **[Async violation] Blocking file I/O** — `att.path.read_bytes()` inside async methods in all three providers (`openai.py:70`, `openai_responses.py:80`, `anthropic.py:116`); must be `hass.async_add_executor_job`.
- **[Correctness risk, Anthropic] `signature_delta` discarded** (`anthropic.py:271-272`) and never replayed on subsequent thinking+tool-use turns — plausibly violates Anthropic's extended-thinking replay requirement (*inferred*, verify against live docs).
- **[Imprecision] `len(text)//4` token estimator** for context pruning (`entity.py:162-193`) — not a real tokenizer; can over-prune or exceed real limits.
- **[Design smell] Full re-serialization per iteration** (`entity.py:484`) — O(n) reconversion up to 10× per turn (required for Anthropic alternation, but not optimized).
- **[Robustness] `CustomServiceTool`** (`entity.py:56-114` / likely now `entity_tools.py`) supports only string/number/integer/boolean (no array/object/enum) and swallows all service errors into `{"error": str(err)}`.
- **[Duplication] `get_models` byte-identical** across `openai.py:269`/`openai_responses.py:285`; attachment→base64 reimplemented 3×; `DeviceInfo` duplicated entity/sensor. **`get_provider()` uses an absolute `custom_components...` import** (`providers/__init__.py:74`).
- **[Config drift] Provider-capability flags** (thinking, response_format) duplicated between `config_flow.py:386-415` conditionals and `providers/*.py`.
- **[Gaps] No non-streaming fallback** for non-SSE backends; **Anthropic `get_models` swallows errors → `[]`** (masks bad key as "no models"); **unbounded `AgentMemoryStore`** re-injected into every prompt with no cap; **AI Task has no usage sensor**; **`build_url` hardcoded** (no gateway-path override); **`hacs.json` floor** unreconciled with used APIs; **`research.md` §8** documents an abandoned OptionsFlow pattern unmarked as superseded.

### Keep / Change / Drop

| Item | Verdict | Notes |
|---|---|---|
| Provider `Protocol` + 3 adapters | **Keep** | Fix absolute import; factor shared `get_models` + attachment helper |
| Entry/subentry + `runtime_data` + agent lifecycle + migration | **Keep** | Matches current best practice |
| Triple-inheritance entity + shared `BaseLLMEntity` + `_async_handle_chat_log` loop | **Keep** | Split `max_iterations` per call site |
| Streaming into `async_add_delta_content_stream` | **Keep** | Ensure early text emission for voice |
| Retry + sticky model-fallback | **Keep** | Well-scoped resilience |
| Unified structured-output routing | **Keep** | Add two-tier native/forced-tool fallback |
| Agent-invoked memory tools (Save/Update/Remove) | **Keep** | Add size cap / relevance filter |
| Dispatcher usage sensor | **Keep** | Extend to AI Task subentries |
| Custom-tool injection onto `chat_log.llm_api.tools` | **Change** | Register a dedicated `llm.API`; decouple from HASS-API selection |
| System-prompt marker splicing for entity context | **Change** | Own the prompt via custom `llm.API` |
| `CONF_MEMORY_ENABLED` rolling buffer | **Change** | Persist (Store) or rename; verify vs `chat_log` continuity |
| Blocking `read_bytes()` | **Change** | `hass.async_add_executor_job` |
| `len//4` token pruner | **Change / Drop** | Real tokenizer, or drop client-side pruning + rely on provider errors |
| Anthropic `signature_delta` discard | **Change** | Persist + replay, or drop thinking block when no signature |
| Provider-capability config conditionals | **Change** | Move flags onto provider classes |
| `hacs.json` `2025.1.0` floor, empty `codeowners`, missing `integration_type`/`quality_scale.yaml` | **Change** | Reconcile + fill |
| Anthropic `get_models` error-swallowing | **Change** | Surface real error to config flow |
| `research.md` §8 OptionsFlow section | **Drop / mark superseded** | Abandoned architecture |
| Adding a `DataUpdateCoordinator` for the agent | **Don't add** | Unearned for stateless per-turn calls |
| Consuming/exposing MCP in-integration | **Don't add** | Stock `mcp`/`mcp_server` already do it |

---

## 8. Recommended target architecture

### Proposed file layout
```
custom_components/llm_home_controller/
  __init__.py            # entry setup, runtime_data session, platform forward, migration
  const.py
  config_flow.py         # ConfigFlow + ConfigSubentryFlow (conversation, ai_task_data)
  entity.py              # BaseLLMEntity(Entity): _async_handle_chat_log (shared loop)
  conversation.py        # ConversationEntity + AbstractConversationAgent + BaseLLMEntity
  ai_task.py             # AITaskEntity + BaseLLMEntity
  sensor.py              # usage sensor (conversation AND ai_task subentries)
  llm_api.py             # NEW: dedicated llm.API hosting this integration's custom tools
  tools.py               # GetEntityDetails, memory CRUD, CustomServiceTool
  memory.py              # AgentMemoryStore (durable, capped)
  history.py             # optional: Store-backed rolling history (if kept, renamed)
  providers/
    __init__.py          # LLMProvider Protocol + get_provider() (relative import)
    base.py              # NEW: shared get_models + attachment→base64 executor helper
    openai.py            # Chat Completions (primary OpenAI-compatible template)
    anthropic.py         # Messages API (+ thinking signature round-trip)
    openai_responses.py  # Responses API (optional; distinct capability flags)
  manifest.json          # + integration_type:"service", codeowners, quality_scale
  quality_scale.yaml     # seeded from openai_conversation, Bronze targets
  strings.json / translations/en.json  # + config_subentries, exceptions keys
  brand/                 # already present
```

### Key classes & tool-calling design
- **`BaseLLMEntity(Entity)`** owns `_async_handle_chat_log(chat_log, *, structure_name=None, structure=None, max_iterations=MAX_TOOL_ITERATIONS)` — one loop, shared by both platforms. Conversation passes `max_iterations=10`; AI Task passes `1000`.
- **Tool exposure via a dedicated `llm.API`** (`llm_api.py`): register once per parent entry with `llm.async_register_api(...)`, hosting `GetEntityDetails` + memory tools + `CustomServiceTool`s and its **own** system prompt for custom entity context. Users select it in `CONF_LLM_HASS_API` alongside (or instead of) `assist` — tools no longer depend on Assist being selected. `async_get_api` merges both into one `APIInstance`.
- **`transform_stream` + `_convert_content`** per provider, plus a **generic delta-accumulator helper** in `providers/base.py` (the finalize-on-boundary shape is near-identical across vendors even though event names differ).
- **Small-model hardening** in `providers/base.py`: `_fix_invalid_arguments` (stringified-JSON args), `_parse_tool_args` (drop empty/None args), `_trim_history` (keep system + last N pairs).
- **Structured output**: two-tier — native `json_schema`/`response_format` when the backend/model is known to support it, else forced synthetic tool call; Anthropic keeps prompt-injection fallback (no native mode) but attempts native output_config where the model supports it.

### Provider abstraction
Keep the 7-method `Protocol`. **Move capability flags onto the provider classes** (`supports_thinking`, `supports_native_structured_output`, `supports_reasoning`) so `config_flow.py` reads them instead of duplicating provider-type conditionals. Add an optional `build_url` path override for nonstandard gateways. Decide deliberately whether a non-streaming fallback is needed for less-compliant proxies.

### Major forks — recommendation + rationale

1. **Subentries vs OptionsFlow** → **Subentries (Recommended).** Upstream-blessed, matches existing architecture, no connection-config duplication. Reversible but no reason to.
2. **Chat Completions vs Responses API (primary)** → **Chat Completions via `ollama/entity.py` template (Recommended).** The actual backends (llama-swap/vLLM/LiteLLM/Ollama) speak Chat Completions; Responses API is stateful and unsupported there. Keep `openai_responses.py` as an optional adapter for users pointing at real OpenAI, but don't make it the structural base.
3. **Custom tools: inject vs dedicated `llm.API`** → **Dedicated `llm.API` (Recommended).** Fixes the silent-drop gap, gives independent prompt control, composes with Assist. Cost: modest boilerplate (`API`/`APIInstance`/registration lifecycle).
4. **Consume/expose MCP** → **Neither in-integration (Recommended).** Stock `mcp`/`mcp_server` already provide both via the same `llm.API` surface; document as companion installs.
5. **Streaming in voice** → **Mandatory `_attr_supports_streaming = True`, emit early text (Recommended).** It's the primary latency lever for agentic voice; the whole value prop is the worst-case (multi-tool) voice UX.
6. **"Memory": rebuild rolling buffer vs rely on `chat_log`** → **Verify `chat_log` per-`conversation_id` continuity first; if a cross-restart buffer is genuinely needed, persist it Store-backed and rename the config option.** Don't ship durability the name implies but the code doesn't deliver.
7. **DataUpdateCoordinator** → **Don't add.** Unearned for stateless per-turn calls; only justified by a live capability endpoint most OpenAI-compatible backends lack.
8. **Client-side token pruning** → **Drop or use a real tokenizer (Recommended: drop for OpenAI-compatible, keep Ollama-style message-count trimming).** The `len//4` heuristic is imprecise; message-count `_trim_history` is simpler and adequate.

### Phased build order
1. **Skeleton**: manifest (`integration_type`, `codeowners`), `__init__` + `runtime_data`, config flow + subentries, migration, quality_scale.yaml, hacs.json reconciliation.
2. **Provider layer**: `Protocol` + `base.py` (shared helpers, small-model repair, async attachments), OpenAI Chat Completions adapter, streaming `transform_stream`. Unit tests per adapter.
3. **Conversation entity + shared loop**: `BaseLLMEntity._async_handle_chat_log`, `_async_handle_message` chain, streaming end-to-end test with real `MockChatLog`.
4. **Dedicated `llm.API`** + custom tools (entity-query, service tools) decoupled from Assist.
5. **Anthropic adapter** (thinking + signature round-trip + native/prompt structured output).
6. **AI Task** platform + structured output two-tier + its own usage sensor.
7. **Memory** (durable, capped) + history decision; usage sensor for both platforms.
8. **Hardening**: reauth-on-401 (Silver), exception translations, docs, retry/fallback polish.

---

## 9. Open questions / decisions needed from the owner

1. **Scope of surfaces:** conversation-only, or conversation + AI Task? (Determines whether the `max_iterations` split and structured-output work are in scope — the repo already has `ai_task.py`, suggesting yes.)
2. **Rolling "memory":** is cross-restart persistence actually wanted, or is per-session `chat_log` continuity sufficient? (Drives whether to keep, persist, or drop the buffer, and whether to rename the config option.)
3. **Backend matrix:** which of llama-swap / Ollama-OpenAI / vLLM / LiteLLM must be first-class, and which advertise `response_format: json_schema`? (Drives the structured-output two-tier fallback and whether a non-streaming path is needed.)
4. **Custom entity-context templating:** keep it as a power-user feature (via a dedicated `llm.API` prompt), or drop it in favor of standard Assist exposure?
5. **HA version floor:** raise `hacs.json` to a realistic floor (≥ the release that shipped `ConfigSubentry` + `ai_task` + `thinking_content`), and should CI test against it?
6. **Quality target:** Bronze-only, or commit to Silver (reauth-on-401, action-exceptions, config-entry-unloading) for the first shipped rewrite?
7. **Anthropic extended thinking + tool use:** is this a supported use case that must round-trip signatures correctly, or acceptable to strip thinking blocks on replay?

---

## 10. References

**Integration fundamentals / config entries / manifest / quality / HACS**
- Config entries — https://developers.home-assistant.io/docs/config_entries_index/
- Integration manifest — https://developers.home-assistant.io/docs/creating_integration_manifest/
- Integration quality scale — https://developers.home-assistant.io/docs/core/integration-quality-scale/
- Fetching data (coordinator) — https://developers.home-assistant.io/docs/integration_fetching_data/
- Device registry — https://developers.home-assistant.io/docs/device_registry_index/
- Config Subentries (arch #1070) — https://github.com/home-assistant/architecture/discussions/1070
- Brands proxy / local brand folder — https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/
- HACS integration publishing — https://www.hacs.xyz/docs/publish/integration/
- `openai_conversation/__init__.py` — https://github.com/home-assistant/core/blob/dev/homeassistant/components/openai_conversation/__init__.py
- `openai_conversation/config_flow.py` — https://github.com/home-assistant/core/blob/dev/homeassistant/components/openai_conversation/config_flow.py

**Conversation / ChatLog / tool loop / LLM helper**
- LLM API for Large Language Models — https://developers.home-assistant.io/docs/core/llm/
- Conversation entity — https://developers.home-assistant.io/docs/core/entity/conversation/
- `conversation/chat_log.py` — https://github.com/home-assistant/core/blob/dev/homeassistant/components/conversation/chat_log.py
- `conversation/entity.py` — https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/conversation/entity.py
- `conversation/util.py`, `models.py`, `agent_manager.py` (dev branch, same repo tree)
- `helpers/chat_session.py` — https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/helpers/chat_session.py
- `helpers/llm.py` — https://github.com/home-assistant/core/blob/dev/homeassistant/helpers/llm.py
- Standardize ChatSession (arch #1191) — https://github.com/home-assistant/architecture/discussions/1191
- `ollama/{conversation,entity,ai_task}.py` (dev branch)

**AI Task**
- AI Task entity (dev docs) — https://developers.home-assistant.io/docs/core/entity/ai-task/
- AI Task (user docs) — https://www.home-assistant.io/integrations/ai_task/
- `ai_task/{__init__,entity,task,const}.py`, `openai_conversation/ai_task.py`, `google_generative_ai_conversation/ai_task.py` (dev branch)
- 2025.8 "summer of AI" — https://www.home-assistant.io/blog/2025/08/06/release-20258/
- Structured-output bug (gemma-3-27b-it) — https://github.com/home-assistant/core/issues/151841

**Voice pipeline**
- Assist pipelines — https://developers.home-assistant.io/docs/voice/pipelines/
- Assist satellite entity — https://developers.home-assistant.io/docs/core/entity/assist-satellite/
- `assist_pipeline/pipeline.py`, `intent/timers.py` (dev branch)
- TTS streaming (disc #2277) — https://github.com/orgs/home-assistant/discussions/2277
- Wyoming — https://www.home-assistant.io/integrations/wyoming/

**MCP**
- `mcp_server` docs — https://www.home-assistant.io/integrations/mcp_server/
- `mcp` (client) docs — https://www.home-assistant.io/integrations/mcp/
- `mcp_server/{__init__,http,server,config_flow,manifest}` and `mcp/{__init__,coordinator,auth}` (dev branch)

**Reference integrations**
- `anthropic/{entity,conversation,ai_task,coordinator,config_flow}.py` (dev branch)
- `openai_conversation/entity.py`, `ollama/entity.py`, `google_generative_ai_conversation/entity.py` (dev branch)

**Testing / distribution**
- Testing (dev docs) — https://developers.home-assistant.io/docs/development_testing/
- Internationalization (backend) — https://developers.home-assistant.io/docs/internationalization/core/
- `tests/components/conversation/__init__.py` (MockChatLog) + PR #138112 — https://github.com/home-assistant/core/pull/138112
- `openai_conversation/quality_scale.yaml` + `tests/.../conftest.py` (dev branch)
- pytest-homeassistant-custom-component — https://github.com/MatthewFlamm/pytest-homeassistant-custom-component

---

## Least confident / needs live verification against running HA 2026.7

1. **`ChatLog.async_update_llm_data` now raises (not warns).** Based on `frame.report_usage(breaks_in_ha_version="2026.1")` in dev-branch source; confirm behavior at the *exact HA version pinned in this repo's lockfile* with `grep -n 'async def async_update_llm_data'`. Load-bearing for the "migrate every call site" claim.
2. **Anthropic extended-thinking signature replay requirement.** Whether replaying a thinking block without its `signature` on a subsequent tool-use turn is actually rejected — **inferred**, not confirmed against current Anthropic docs. Gate the `anthropic.py:271-272` fix on this.
3. **Whether local OpenAI-compatible backends honor `response_format: json_schema`** the way OpenAI does, or silently ignore it. Determines whether the two-tier structured-output fallback is required. Verify per target backend (llama-swap/vLLM/LiteLLM) with a live probe.
4. **`developers.home-assistant.io`'s `<integration>/llm.py` platform (`async_get_tools`/`llm.LLMTools`)** — documented but not confirmed against live `helpers/llm.py`; possible doc drift. Verify before designing the dedicated-`llm.API` around it (the `async_register_api` path is confirmed and is the safer choice).
5. **`hassfest` brands check passing for a HACS-only integration via the local `brand/` folder alone** (no `home-assistant/brands` entry, no manifest `brand` key). The `brand/` files exist, but that hassfest actually passes on this repo was **NOT run** — verify with `python3 -m script.hassfest` in the HA Core Functional Test container.
6. **`hacs.json` `2025.1.0` floor vs. runtime APIs used** — a user on that floor could hit a hard failure from `ConfigSubentry`/`ai_task`/`thinking_content`; the exact release that introduced each was not pinned down here.
7. **Exact current `MockChatLog` shape** on the pinned dev branch (it moved location/internals at least once, PR #138112) — re-diff before vendoring.
8. **Whether the existing rolling-history buffer duplicates `chat_log` per-`conversation_id` continuity** — not verified; drives the memory-rebuild decision.

**TL;DR:** The repo already implements the current HA-blessed agentic-LLM shape; the rewrite is a consolidation. Highest-leverage moves: register a dedicated `llm.API` to fix the silent custom-tool drop, stop splicing HA's system prompt, fix the "memory" persistence lie, make attachment I/O async, adopt Ollama's small-model hardening, split `MAX_TOOL_ITERATIONS` per surface, and formalize quality/distribution (integration_type, codeowners, quality_scale.yaml, hacs floor). Use `ollama/entity.py` (not `openai_conversation`) as the OpenAI-compatible template; keep AI Task; leave MCP to the stock integrations.
