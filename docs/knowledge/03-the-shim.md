# 03 — The Shim (HA-side passthrough conversation agent)

Everything we worked out about **the shim**: the thin Home Assistant conversation agent that plugs
into Assist/Voice and *forwards* each turn to an external agent instead of running an LLM in-process.

> **Terminology.** Across our sessions "the shim" and "the middleman" got used loosely. Precisely:
> - **Shim** = the HA-side `ConversationEntity` (this doc). It lives in a **HACS custom integration**
>   (`custom_components/<domain>/`). It is thin: it owns HA plumbing, not intelligence.
> - **Middleman** = the external **FastAPI service** (the brain). See `docs/plans/middleman-implementation-brief.md`.
> - The **contract** between them is §4 below (and repeated in the middleman brief so both sides match).

---

## 1. What the shim is (and is not)

**Is:** a Home Assistant conversation agent entity that receives the recognized utterance from the
Assist pipeline, POSTs it to the middleman, and streams the reply back into the pipeline (→ TTS). It
holds: config flow, entity lifecycle, `conversation_id` handling, the streaming-delta translation,
and graceful fallback.

**Is not:** an LLM client, a tool executor, or an agent loop. All of that lives in the middleman.
The whole point of the passthrough is to keep heavy agent machinery (and its dependency tree) *out*
of Home Assistant's Python environment.

**Why a shim at all** (vs. HA's built-in LLM conversation integrations): so the "brain" can be an
external service with its own dependencies, release cadence, scaling, and — if desired —
LangGraph/deep-agent capability, **without** any of that living inside HA. HA keeps what it's best
at (the voice front-end); the brain lives where it can't bloat or destabilize HA.

---

## 2. Where the shim lives (HACS structural reality)

A HA integration **must** be a `custom_components/<domain>/` package with a `manifest.json`. It
cannot be a FastAPI service. And **HACS enforces exactly one integration per repository** (verbatim:
*"There must only be one integration per repository … only the first one will be managed"*).

Consequences for *this* project:
- The shim needs a **HACS-structured home** — either its own repo, or folded into the
  `LLM-Home-Controller` rewrite as a new *passthrough agent type* (recommended, since two related
  integrations can't share one HACS repo). See `05` §"Where the shim lives".
- **The current `LLM-Middleman` repo is a FastAPI `service` scaffold** (the middleman). If you intend
  to build the *HA shim* here, the repo would need restructuring to `custom_components/<domain>/` +
  HACS layout — a different archetype than what was scaffolded. Flag/confirm this before building.

Minimal shim integration layout (once it has a HACS home):
```
custom_components/<shim_domain>/
  __init__.py        # entry setup; store aiohttp session + middleman config in runtime_data
  manifest.json      # domain, name, version (required for custom), integration_type, codeowners,
                     # dependencies: ["conversation"], iot_class: local_push
  config_flow.py     # ConfigFlow (+ ConfigSubentryFlow if multiple agents) — middleman URL, auth token
  const.py
  conversation.py    # the ConversationEntity shim (the core of this doc)
  strings.json / translations/en.json
  brand/             # icons (local brand folder, HA 2026.3+)
```

---

## 3. How the shim plugs into HA (the plumbing — all from `01`)

The HA API facts below are the load-bearing ones; full detail + exact signatures are in `01` §3.

- **Entity class:** `ConversationEntity + AbstractConversationAgent` (+ a small base if you want).
  `ConversationEntity` extends `RestoreEntity`. Register with
  `conversation.async_set_agent(hass, entry, agent)` in `async_added_to_hass`; unset on unload.
- **Override only `_async_handle_message(self, user_input, chat_log) -> ConversationResult`.** Never
  `async_process` / `internal_async_process` (`@final`).
- **The turn chain** inside `_async_handle_message`:
  1. `chat_log.async_provide_llm_data(...)` — for a **pure passthrough you may skip HA-side tools
     entirely** (the middleman gets its tools from HA via MCP, §4). If you *do* want HA to also expose
     Assist tools locally, this is where the `CONF_LLM_HASS_API` resolution happens. Decision in `05`.
  2. Your forwarding logic (§4) — POST to the middleman, stream deltas into `chat_log` via
     `chat_log.async_add_delta_content_stream(agent_id, <async delta generator>)`.
  3. `conversation.async_get_result_from_chat_log(user_input, chat_log)` — builds the
     `ConversationResult`. The last content **must** be `AssistantContent` or it raises.
- **`conversation_id` + session lifetime** are handled by HA (`helpers/chat_session`, fresh ULID,
  5-min timeout). You just **pass `conversation_id` out to the middleman** so it can key its own
  per-session state. Nothing to implement HA-side.
- **`continue_conversation`** is computed by HA (assistant message ending in `?`/`？`/`;`). If you
  want reliable follow-up prompting, ensure the middleman's final text ends accordingly, or set it
  explicitly if the API allows.
- **`ConverseError`** is for **pre-flight** failures only (won't be stored in history). Mid-turn
  failures should surface as visible assistant/tool content, or a graceful fallback message.

---

## 4. The contract: shim ⇄ middleman (THE interface)

Recommended transport: **HTTP POST with a Server-Sent Events (SSE) response**. Rationale: matches
HA's streaming idiom, trivial to consume from `aiohttp` in the shim, one-directional is all a turn
needs. (WebSocket only if you later need mid-turn bidirectional signalling.)

**Request** `POST /v1/converse` (`Authorization: Bearer <shared secret>`):
```jsonc
{
  "conversation_id": "01J…",     // HA session key; null on a new conversation
  "text": "turn off the kitchen lights",
  "language": "en",
  "device_id": "…",              // optional; lets the middleman apply a shallower iteration cap for voice
  "context": { "area": "kitchen" } // optional grounding the shim can add
}
```

**Response** `text/event-stream`:
```
event: text_delta
data: {"delta": "Turning off the kitchen lights."}

event: tool_activity            # optional — observability/logging only
data: {"tool": "HassTurnOff", "args": {"name": "kitchen lights"}, "status": "ok"}

event: done
data: {"text": "Turning off the kitchen lights.", "continue_conversation": false}

# error path
event: error
data: {"code": "backend_unavailable", "message": "…"}
```

**Shim responsibilities on this stream:**
- Convert each `text_delta` → HA `AssistantContentDeltaDict`
  (`{"role":"assistant","content": "<chunk>"}`) fed into `async_add_delta_content_stream` so TTS can
  start early.
- Ensure the **final** `chat_log` content is an `AssistantContent` (the `done.text`), so
  `async_get_result_from_chat_log` succeeds.
- On `error`/timeout → emit a graceful fallback assistant message (or delegate to HA's default agent)
  — never leave the pipeline hanging.
- Present a bearer token to the middleman; treat the user `text` as untrusted.

---

## 5. Latency & voice UX (the hard constraints)

The whole value proposition is acceptable voice UX with an agentic brain. What we learned:

- **Stream text deltas** the instant they arrive. HA starts feeding TTS after
  `STREAM_RESPONSE_CHARS = 60` accumulated characters **if** `_attr_supports_streaming = True` *and*
  the TTS engine supports streaming input — so the assistant can speak before the tool loop finishes.
  **Set `_attr_supports_streaming = True` on the shim entity.**
- **The unavoidable gap:** when the *first* useful text requires a tool result (e.g. "what's the
  temperature?"), streaming can't help — the model must call the tool before it can say anything.
  There is **no first-class "filler utterance" primitive** in HA 2026.7. Mitigations: have the
  middleman emit a short preamble ("One moment…") as an early `text_delta`, and/or keep voice turns
  shallow.
- **Keep voice turns shallow** (small middleman iteration cap for `device_id`-originated turns).
  Route genuinely long-horizon work to a non-voice/AI-Task path.
- **The intent stage runs local sentence-triggers/intents *before* the LLM agent** (timers, common
  device commands bypass the slow path). **Do not** try to own timers/simple intents in the shim.
- **Hard per-turn deadline** in the shim; on timeout, fall back. HA voice is degraded when the
  middleman is down — design for graceful failure and fast `/readyz` on the middleman side.

---

## 6. What the shim deliberately does NOT do

- No LLM calls, no provider adapters, no tool execution loop (all in the middleman).
- No timer/simple-intent handling (HA's intent stage owns that).
- No `conversation_id`/session TTL bookkeeping (HA owns that).
- No `DataUpdateCoordinator` (stateless per-turn; nothing to poll).
- No embedded MCP server/client (the *middleman* is the MCP client to HA; the shim just forwards
  text). See `04` for how control flows.

---

## 7. Open shim-side decisions (see `05` for recommendations)

1. **Transport:** SSE-over-HTTP (recommended) vs WebSocket.
2. **HA-side tool exposure:** pure passthrough (middleman gets all tools via MCP) vs. *also* letting
   the shim expose Assist tools locally through `async_provide_llm_data`. Pure passthrough is simpler
   and keeps one control path.
3. **Single agent vs subentries:** one shim entity, or a subentry pattern for multiple middleman
   endpoints/personas.
4. **Where it lives:** own HACS repo vs. folded into the `LLM-Home-Controller` rewrite as a
   passthrough agent type.
