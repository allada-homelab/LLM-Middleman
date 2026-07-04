# 03 — The Integration (HA-side multi-backend conversation agent)

Everything about **LLM Middleman**: the Home Assistant conversation agent that plugs into
Assist/Voice and *forwards* each turn to an external LLM backend instead of running an LLM
in-process. In v1 it is **backend-agnostic** — one entity over a **backend-preset** adapter
layer. The custom `/v1/converse` SSE contract that v0 spoke exclusively is now **one of five
presets**, not the boundary.

> **Terminology.** `LLM-Middleman` (this repo) is the HA-side `ConversationEntity` described
> here; domain `llm_middleman`. It's thin: it owns HA plumbing, not intelligence. Each
> **backend** it forwards to runs the model, holds memory, and (for text-only presets) owns
> its own tools. The custom `/v1/converse` preset can point at a **separate external-agent
> service** (spec'd in `docs/external-agent-handoff/`, where it is called "the middleman"
> for historical reasons — same word, different thing); that service is now just one backend
> type HA can target, not a required component.
>
> **Text-only.** The integration forwards *text* (HA does STT/TTS). An audio-passthrough /
> speech-to-speech mode was evaluated and **dropped** — it isn't supported in Assist 2026.7
> (see `05` Decision "Text-only").

---

## 1. What it is (and is not)

**Is:** a Home Assistant conversation agent that receives the recognized utterance from the
Assist pipeline, forwards it to the configured backend, and streams the reply back into the
pipeline (→ streaming TTS). It owns: the config flow, entity lifecycle, `conversation_id` /
memory-scope key derivation, the streaming-delta translation, an optional bounded HA tool
loop, and graceful fallback.

**Is not:** an LLM client, a provider SDK, or (for text-only presets) a tool executor. The
intelligence lives in the backend. The whole point is to keep heavy agent machinery (and
its dependency tree) *out* of Home Assistant's Python environment.

**Why forward at all** (vs. HA's built-in LLM conversation integrations): so the "brain" can
be an external service — self-hosted OpenAI-compatible server, Ollama, a LangGraph
deployment, an n8n workflow, or a bespoke agent — with its own dependencies, release cadence,
and scaling, **without** any of that living inside HA.

---

## 2. Repo layout (as built)

`LLM-Middleman` is a HACS conversation-agent integration (domain `llm_middleman`), built and
gate-green. Actual layout (verify against the filesystem — code wins):
```
custom_components/llm_middleman/
  __init__.py        # entry setup: build the adapter via BACKEND_TO_CLS → entry.runtime_data;
                     #   forward CONVERSATION platform; async_migrate_entry (v0 → parent + subentry)
  const.py           # DOMAIN, CONF_* keys, BACKEND_* type constants, defaults
  config_flow.py     # parent flow (backend-type menu → per-backend connection step, probe-validated)
                     #   + the conversation subentry flow (per-agent options)
  conversation.py    # ONE backend-agnostic entity: tool loop + never-hangs guard + chat_log wiring
  diagnostics.py     # redacted config-entry diagnostics
  backends/
    __init__.py      # BACKEND_TO_CLS: dict[str, type[BackendAdapter]] factory
    base.py          # BackendAdapter ABC, TurnContext, shared types + timeout helper
    _sse.py          # spec-compliant SSE reader (shared)
    _history.py      # stateless-replay history helpers (chat_log → provider messages + trim)
    openai_compat.py # /v1/chat/completions SSE; stateless replay; HA-tool capable
    ollama.py        # /api/chat NDJSON; stateless replay + trim
    langgraph.py     # /threads + /threads/{id}/runs/stream (messages-tuple); stateful; text-only
    converse.py      # custom /v1/converse SSE contract; stateful; text-only
    n8n.py           # Chat Trigger / Webhook; NDJSON StructuredChunk + blocking fallback; stateful
  manifest.json      # domain llm_middleman, integration_type "service", dependencies ["conversation"],
                     #   iot_class "local_polling", config_flow true (NO runtime requirements — pure stdlib+HA)
  strings.json / translations/en.json
  brand/             # icons (local brand folder, HA 2026.3+)
tests/               # conftest (MockChatLog + fixtures) + per-backend fake-stream harnesses
```

**HACS note (why its own repo):** a HA integration must live at `custom_components/<domain>/`,
and **HACS allows exactly one integration per repository** — so this integration gets its own
repo. An external-agent service (if you run the `/v1/converse` preset against one) lives in
yet another repo.

---

## 3. How it plugs into HA (the plumbing — detail in `01` §3)

The load-bearing HA-API facts (full signatures in `01` §3):

- **Entity class:** `ConversationEntity` (+ `AbstractConversationAgent`, which core
  openai/ollama still subclass). Register with `conversation.async_set_agent(hass, entry,
  agent)` in `async_added_to_hass`; unset on removal. One entity per `conversation`
  subentry.
- **Override only `_async_handle_message(self, user_input, chat_log) -> ConversationResult`.**
  Never `async_process` / `internal_async_process` (`@final`).
- **The turn chain** inside `_async_handle_message`:
  1. `chat_log.async_provide_llm_data(...)` — passes the agent's selected HA LLM API(s)
     (a list, or `None`), the system prompt, and any `extra_system_prompt`. For text-only
     presets the LLM-API list is `None` (no HA tools). A `ConverseError` here (pre-flight)
     returns `err.as_conversation_result()`.
  2. A bounded tool loop: drive `chat_log.async_add_delta_content_stream(entity_id,
     _guarded(adapter.stream_turn(...)))`, let HA run any tool calls the backend emitted,
     and re-enter while the last content is an unresponded tool result. Text-only presets
     (and turns with no tool call) do exactly one iteration; the cap
     (`MAX_TOOL_ITERATIONS = 10`) is the HA-side backstop against a runaway backend.
  3. `conversation.async_get_result_from_chat_log(user_input, chat_log)` builds the
     `ConversationResult`. The last content **must** be `AssistantContent` or it raises —
     the guard (below) guarantees that.
- **`conversation_id` + session lifetime** are HA's (`helpers/chat_session`, fresh ULID,
  ~5-min timeout). The entity derives a **memory key** from it per `CONF_MEMORY_SCOPE` and
  hands it to stateful adapters; nothing to persist HA-side for the default scope.
- **`ConverseError`** is for **pre-flight** failures only. Mid-turn failures surface as a
  graceful fallback assistant message via the guard, never a hang.
- **Built-in Assist chat renders for free.** Because it is a real `ConversationEntity`
  writing to `ChatLog`, its content streams out as the pipeline's `INTENT_PROGRESS` /
  `INTENT_END` events and the frontend renders it with no extra work.

**Never-hangs guard (`_guarded`).** Every adapter stream is wrapped so at least one
`AssistantContent` is emitted on every exit path: it injects a leading `{"role":
"assistant"}` if the first delta lacks a role, catches `Exception` broadly (the v0 holes
were `ValueError` from aiohttp's 64 KB readline cap and `UnicodeDecodeError`, plus
`TimeoutError`), logs with `_LOGGER.exception`, and appends the fallback message. A silent
stream end also yields role + fallback.

**Timeouts.** `aiohttp.ClientTimeout(total=CONF_TIMEOUT, sock_read=IDLE_TIMEOUT)` — a
per-agent configurable total (default 60 s; n8n defaults lower) plus an idle-read timeout
(~30 s) so a responsive-but-slow stream isn't killed. This replaces v0's single total
deadline that killed slow-but-streaming backends.

---

## 4. The backend presets

Five adapters register in `BACKEND_TO_CLS`, each normalizing its provider's stream into the
**canonical delta shape** the entity consumes: the first delta of an assistant block carries
`{"role": "assistant"}`, then each chunk carries `{"content": <text>}` (tool-capable presets
additionally emit tool-call deltas). Two axes distinguish them: **stateless-replay vs
stateful-thread**, and **SSE vs NDJSON**.

| Preset | Endpoint / transport | State | HA tools | Follow-up override |
|--------|----------------------|-------|----------|--------------------|
| **openai_compat** | `/v1/chat/completions`, SSE (`[DONE]`) | stateless (replays chat_log) | **yes** (`supports_ha_tools = True`) | — (automatic `?` only) |
| **ollama** | `/api/chat`, NDJSON (`done: true`) | stateless (replay + trim) | not yet¹ | — (automatic `?` only) |
| **langgraph** | `/threads/{id}/runs/stream`, SSE `messages-tuple` | stateful (memory_key → thread_id) | no | — (automatic `?` only) |
| **converse** | `/v1/converse`, SSE `text_delta`/`done`/`error` | stateful (backend owns history) | no | `done.continue_conversation` |
| **n8n** | Chat Trigger / Webhook, NDJSON `StructuredChunk` (+ blocking fallback) | stateful (`sessionId`) | no | `continueConversation` (blocking) |

¹ `ollama.py` sets `supports_ha_tools = False` in this build; its native `/api/chat`
protocol carries tool calls, and wiring them into the HA tool loop is tracked as a separate
ticket. As of this writing **openai_compat is the only preset with `supports_ha_tools =
True`** — cross-check `backends/*.py` before claiming otherwise.

**The custom `/v1/converse` contract (the `converse` preset).** This is the v0 contract,
now one preset among five. It is the cleanest fit for a text-only voice turn and defines the
internal canonical delta shape. The backend owns conversation history, keyed on the
forwarded session key.

**Request** `POST {base_url}/v1/converse` (optional `Authorization: Bearer <token>`):
```jsonc
{
  "conversation_id": "01J…",   // the entity's derived memory key (per memory_scope)
  "text": "turn off the kitchen lights",
  "language": "en",
  "device_id": "…"             // included only when the turn has one
}
```
> **No `context` field.** v0 docs described an optional `context: {area: …}` grounding
> object. The adapter **never sends it** (see `backends/converse.py::stream_turn` — the body
> is exactly the four fields above). Do not build a backend that expects it.

**Response** `text/event-stream`, framed by the spec-compliant SSE reader
(`backends/_sse.py`), not v0's per-line loop:
```
event: text_delta
data: {"delta": "Turning off the kitchen lights."}

event: done
data: {"text": "Turning off the kitchen lights.", "continue_conversation": false}

# error path
event: error
data: {"code": "backend_unavailable", "message": "…"}
```
Adapter handling (`converse.py`):
- `text_delta` → canonical role-first content deltas fed to `async_add_delta_content_stream`
  so TTS starts early.
- `done` → **terminate.** The streamed `text_delta`s are authoritative; `done.text` is used
  **only** as the reply when nothing streamed (a backend that produced text without
  streaming any delta). `done.continue_conversation` truthy → the adapter sets
  `ctx.continue_conversation`, which the entity ORs into the `ConversationResult` (§6).
- `error` or non-200 → raise `BackendStreamError`; the guard turns it into the fallback.

Untrusted stream content is treated as data, never parsed as instructions; bearer/x-api-key
values are redacted from logs and diagnostics.

---

## 5. Configuration model (parent entry + subentries)

Core's 2025.8+ parent-entry + subentries pattern (openai/ollama template):

- **Parent flow** — step 1 is a backend-type `SelectSelector` over `BACKEND_TO_CLS` keys;
  step 2 is that backend's connection form (URL normalized; auth shape per backend), which
  is validated by the adapter's `async_validate_connection` probe against the **real**
  endpoint (openai `GET /v1/models`, ollama `GET /api/tags`, langgraph `GET /ok`, converse a
  transport-level reachability check, n8n the webhook). There is **no URL-based unique_id**
  — two agents on one backend are legitimate (a v0 defect fixed).
- **Conversation subentry flow** — one typed `conversation` subentry per agent, with
  `async_step_user` (new) and `async_step_reconfigure` (edit) aliasing one shared
  `set_options` step. Per-agent options: name (the subentry title), system prompt, model
  (dropdown for catalog backends, free-text fallback), timeout, and — capability-gated —
  `CONF_MEMORY_SCOPE` (only when `adapter.supports_memory_scope`) and `CONF_LLM_HASS_API`
  (multi-select, only when `adapter.supports_ha_tools`).
- **Migration** — config-entry `VERSION = 2` + `async_migrate_entry` converts a v0 flat
  entry (`url` / `token` / `system_prompt`) into a `converse` parent + one conversation
  subentry, re-keying the existing entity and device from `entry_id` to the new
  `subentry_id` so the `entity_id` (and the automations/exposure referencing it) survive.

---

## 6. Follow-up listening (agent clarifying questions)

Verified against the installed HA source: `ChatLog.continue_conversation` is true when the
last assistant message ends with `?` / `；` / `？`, and `async_get_result_from_chat_log`
copies it into `ConversationResult.continue_conversation`, which voice satellites use to keep
the mic open for a wake-word-free follow-up.

- **Automatic (all presets):** any reply ending in a question mark keeps HA listening —
  free, no protocol slot needed.
- **Explicit override:** the entity ORs an adapter-provided flag into the result. The
  `converse` preset honors `done.continue_conversation` (finally wiring the field v0
  documented but ignored); n8n honors a `continueConversation` field in a blocking reply.
  OpenAI-compatible / Ollama / LangGraph have no protocol slot, so they rely on automatic
  `?`-detection only.
- The follow-up turn arrives with the **same** HA `conversation_id` (5-min session TTL), so
  backend context holds across the clarify → answer loop.

---

## 7. Memory scope (thread identity)

For **stateful** backends a per-agent `CONF_MEMORY_SCOPE` controls the session key the
entity derives and hands the adapter:
- `conversation` (default) — key = HA `conversation_id`; continuity within the session,
  fresh thread after the TTL. Matches Assist semantics.
- `device` — key = `user_input.device_id` (falls back to conversation scope when the turn
  has no device): each satellite/room keeps one long-lived thread.
- `agent` — key = the subentry id: one continuous thread for the whole agent.

Per connector: LangGraph maps the key → `thread_id`; n8n sends it as `sessionId`; converse
sends it as the request's `conversation_id`. **Honest limit:** stateless backends
(OpenAI-compatible, Ollama) derive context by replaying HA's ChatLog, which only spans the
HA session — they cannot resurrect history HA discarded, so `memory_scope` is hidden for
them (capability flag `supports_memory_scope = False`).

---

## 8. Latency & voice UX (the hard constraints)

- **Stream deltas** the instant they arrive. HA starts feeding streaming TTS after
  `STREAM_RESPONSE_CHARS = 60` accumulated characters when `_attr_supports_streaming = True`
  and the TTS engine streams — cutting time-to-first-audio from >5 s to **~0.5 s**. The
  entity sets `_attr_supports_streaming = True`.
- **STT returns a single final transcript** (no interim words), so the entity forwards one
  complete text per turn.
- **The unavoidable gap:** when the first useful text requires a tool result (e.g. "what's
  the temperature?"), streaming can't help — the model must call the tool first. There is no
  first-class "filler utterance" primitive in HA 2026.7. Mitigations: have the backend emit
  a short preamble as an early delta, and/or keep voice turns shallow.
- **Local intents run first.** HA's intent stage handles sentence triggers and (when
  enabled) `prefer_local_intents` **before** the conversation agent — so timers and simple
  device commands never reach this integration (see §9). Do not try to own them here.
- **Hard per-turn deadline** with graceful fallback (§3). Voice is degraded when the backend
  is down — design the backend for fast failure.

---

## 9. What it deliberately does NOT do

- No LLM calls or provider SDKs; for text-only presets, no tool execution loop (the backend
  owns tools server-side).
- No timer / simple-intent handling — HA's intent stage owns that, ahead of the agent. A
  matched local intent legitimately never reaches the agent; this is expected, not a
  dropped message.
- No `conversation_id` / session-TTL bookkeeping (HA owns it; the entity only derives a
  memory key from it).
- No `DataUpdateCoordinator` (stateless per turn; nothing to poll).
- No embedded MCP server — HA's own `mcp_server` (or a backend's own MCP client) covers that
  path; the tool-capable presets instead expose HA tools to the backend via
  `CONF_LLM_HASS_API` and run the loop in-entity.

---

## 10. Decisions now settled by v1 (were "open" in v0)

The v0 draft of this doc listed several shim-side decisions as open. v1 resolves them:
1. **Transport:** per preset — SSE (openai_compat / langgraph / converse), NDJSON (ollama /
   n8n). Not a single global choice.
2. **HA-side tool exposure:** optional per agent via `CONF_LLM_HASS_API` on tool-capable
   presets (openai_compat today); text-only presets keep tools server-side. Not an
   all-or-nothing repo decision.
3. **Single agent vs subentries:** parent connection + multiple conversation subentries.
4. **Where it lives:** its own HACS repo (`LLM-Middleman`).

**Still open (owner decision, not resolved by v1):** whether to rename the repo to kill the
"middleman = this HA integration vs middleman = the external brain" naming collision (see
`06` glossary). Flagged for the owner; not changed here.
