# External Agent Service — Implementation Brief

> **You are building the external agent ("the brain") in its OWN new repo.** The Home Assistant side
> — a thin conversation-agent shim called `LLM-Middleman` — is already built and forwards each turn
> to you over the `/v1/converse` SSE contract (§4). This brief is self-contained; companion files in
> this bundle cover the LLM provider layer (`llm-providers.md`) and the HA control path
> (`mcp-to-home-assistant.md`).
> **Prior art (reference only, not required on disk):** `LLM-Home-Controller` — an in-HA LLM
> integration whose provider/adapter patterns are inlined into `llm-providers.md`.

---

## 1. The architecture (the "why")

```
┌─────────────────────────── Home Assistant (owns the voice front-end) ───────────────────────────┐
│   HA mic / Voice satellite / VPE                                                                 │
│        │  wake word → STT                                                                         │
│        ▼                                                                                          │
│   Assist pipeline ──► conversation agent = the SHIM (thin ConversationEntity — already built)     │
│        ▲                    │  forwards recognized TEXT (+ conversation_id)                        │
│        │  TTS ◄── streamed  │                                                                      │
│        │  text deltas       ▼                                                                      │
│        │              ┌───────────────── HTTP/SSE contract (§4) ─────────────────┐                │
│        │              │                                                          │                │
│   HA `mcp_server` ◄───┼──── MCP client (control the home) ◄──┐                   │                │
│   (Assist tools       │                                      │                   ▼                │
│    over HTTP)         │                        ┌───────── External Agent Service (this repo) ─────┐│
│                       └──── streamed reply ◄────┤  FastAPI service                                ││
│                                                 │   • agent loop over an LLM                      ││
└─────────────────────────────────────────────────┤   • OpenAI-compatible + Anthropic providers    ││
                                                   │   • MCP client → HA tools                       ││
                                                   │   • (optional) LangGraph deep agents            ││
                                                   │   • session/memory keyed by conversation_id     ││
                                                   └───────────────────────────────────────────────┘│
```

**Why passthrough instead of an in-HA agent?** Putting a heavy agent framework (e.g. LangGraph /
deepagents) *inside* HA's Python environment fights HA's `ChatLog` (which wants to own the tool loop)
and bloats HA's pinned dependency tree. A passthrough keeps HA's excellent voice front-end, keeps
your dependencies in your own container/release cadence, and lets you use LangGraph **outside** HA
where it doesn't fight anything. HA ships both halves you need: a pluggable `ConversationEntity`
(voice in/out — the shim) and `mcp_server` (you reach back for control).

**Design decisions already made (context, not up for re-litigation unless the owner reopens):**
- Passthrough over embedding an agent framework in HA. ✔
- Home control via HA's built-in `mcp_server` (you are the MCP client). ✔
- Streaming is **mandatory** for acceptable voice latency (early TTS). ✔
- LangGraph deep agents, *if used*, live here (outside HA) and primarily for **autonomous /
  non-voice** multi-step work — not the latency-sensitive voice hot path. ✔
- **Text-only.** The shim forwards *text*; HA owns STT/TTS. Audio never reaches you. (An
  audio-passthrough / speech-to-speech mode was evaluated and dropped — unsupported in Assist 2026.7.)

---

## 2. Home Assistant facts you need

You don't need HA internals to build this service, but you need the two integration points.

### 2.1 The Assist voice pipeline
`wake word → STT → conversation agent → TTS`. The conversation agent is a pluggable entity. The shim
*is* that agent; instead of calling an LLM, it forwards the recognized text to you and streams your
reply back into TTS.

Key HA behaviors your service must respect:
- **`conversation_id`** — HA assigns/continues a conversation id across turns in a session. It is the
  session key. You maintain your own per-`conversation_id` state (history/memory).
- **Streaming** — the shim streams your `text_delta` events into HA. HA's streaming TTS (shipped
  2025.10, "Voice Chapter 11") starts speaking after ~60 accumulated characters, giving **~0.5 s
  time-to-first-audio** — but only if you **stream** text as it's produced. Buffer the whole answer
  and you lose that.
- **STT gives a single final transcript** (no interim words), so you always receive complete `text`.
- **`continue_conversation`** — HA can keep the mic open for a follow-up. Signal it in your `done`
  event when you asked a clarifying question.

### 2.2 Controlling the home
You control HA by acting as an **MCP client** to HA's built-in **`mcp_server`**, which exposes HA's
Assist tools (intents + the entities the user has *exposed to Assist*). **The concrete how — endpoint,
auth, listing/calling tools, mapping them into your LLM's tool schema — is in
`mcp-to-home-assistant.md`.** The control surface is only entities exposed to Assist (your safety
boundary). A REST/WebSocket fallback is covered there too.

---

## 3. What your service does per turn (behavioral spec)

1. Receive `{ conversation_id, text, language, ... }` from the shim (§4).
2. Load/attach session state for `conversation_id` (prior turns / memory).
3. Run a **bounded tool-calling loop** against an LLM:
   - Tools = HA tools discovered via the MCP client (+ any local tools you add).
   - The LLM may call tools (control the home / query state); execute them via MCP; feed results
     back; continue until the LLM produces a final answer or the iteration cap is hit.
   - **Voice turns should be shallow** (small max-iterations, e.g. 3–5) — a human is waiting.
4. **Stream** assistant text deltas back as they're generated (before the whole answer is done).
5. Emit a final event (full text, optional `continue_conversation`).
6. Persist updated session state.

Autonomous / non-voice jobs (if in scope) can use a **much higher** iteration budget and optionally a
LangGraph deep-agent graph — kept separate from the voice path.

---

## 4. The contract: shim ⇄ external agent  (THE critical interface — FROZEN)

The HA shim already implements the consumer side of this. Do not change it unilaterally.

**Transport: HTTP POST with a Server-Sent Events (SSE) response stream.** SSE matches HA's streaming
idiom and is one-directional (request → streamed response), which is all a turn needs.

**Endpoint:** `POST /v1/converse`  (auth: `Authorization: Bearer <shared secret>`, see §8)

**Request body (JSON):**
```jsonc
{
  "conversation_id": "01J...",      // HA session key; null on a brand-new conversation
  "text": "turn off the kitchen lights",
  "language": "en",
  "device_id": "…",                 // optional: originating satellite/device
  "context": {                      // optional: extra grounding the shim may supply
    "area": "kitchen"
  }
}
```

**Response: `text/event-stream`, a sequence of SSE events** (`event:` + JSON `data:`):
```
event: text_delta
data: {"delta": "Turning off "}

event: text_delta
data: {"delta": "the kitchen lights."}

event: tool_activity          # optional, for logging/observability only
data: {"tool": "HassTurnOff", "args": {"name": "kitchen lights"}, "status": "ok"}

event: done
data: {"text": "Turning off the kitchen lights.", "continue_conversation": false}
```
Error case:
```
event: error
data: {"code": "backend_unavailable", "message": "…"}
```

**What the shim does with this stream** (so you know your consumer):
- Converts each `text_delta` → an HA assistant-content delta for early TTS.
- On `error` or timeout → falls back (canned message, or HA's default agent). So **fail fast with a
  clear `error` event** rather than hang.
- Passes `conversation_id` through unchanged; treats `null` as "new session".

---

## 5. Service responsibilities (what to build)

1. **API layer** — a router implementing `POST /v1/converse` (§4) on top of a FastAPI `create_app()`.
   Add `/healthz` (always 200) and `/readyz` (report degraded when a backend/HA is unreachable).
2. **Config** (pydantic-settings, e.g. `src/<your_package>/config.py`) — env-driven:
   - `HA_BASE_URL`, `HA_TOKEN` (long-lived), `HA_MCP_URL` (or derive from base URL — see
     `mcp-to-home-assistant.md`).
   - LLM providers: base URL(s) + API key(s) + model name(s) for **OpenAI-compatible** and
     **Anthropic**; primary/fallback model chain; temperature; `MAX_TOOL_ITERATIONS` (voice) and a
     separate (higher) budget for autonomous tasks.
   - `SHIM_AUTH_TOKEN` (shared secret the shim presents).
   - Timeouts (per-turn deadline, per-LLM-call, per-tool-call).
3. **LLM provider layer** — streaming chat with tool-calling for **OpenAI-compatible** (Chat
   Completions; llama-swap / Ollama / vLLM / LiteLLM) and **Anthropic** (Messages API). Full patterns,
   streaming shapes, and hardening are in **`llm-providers.md`**.
4. **MCP client → HA** — connect to HA `mcp_server`, list tools, execute the tool calls the LLM
   requests, thread results back. Full guide in **`mcp-to-home-assistant.md`**.
5. **Agent loop** — bounded tool-calling loop, streaming text deltas out; shallow for voice.
6. **Session / memory** — keyed by `conversation_id`; bounded. Simple in-process store for v1; a
   LangGraph checkpointer or a persistent store if cross-restart memory is wanted (owner decision).
7. **Observability** — structured logs of LLM I/O, tool calls, and per-stage latency. Latency
   visibility is critical for a voice UX.

---

## 6. Latency & UX requirements (non-negotiables for voice)

- **Stream text deltas** the moment the LLM emits them → early TTS. Do not buffer the whole answer.
- **Keep voice turns shallow.** Deep reasoning + a network hop + tool round-trips = a human waiting in
  silence. Route long-horizon work to a non-voice/autonomous path.
- **Hard per-turn deadline** with a clear `error` event so the shim can fall back — never hang.
- Consider an early **filler/ack** delta ("One moment…") if a turn will visibly stall on tool calls.
- **Reliability coupling:** when this service is down, HA voice is degraded (the shim falls back).
  Design for graceful failure and fast `/readyz` degradation.

---

## 7. Scaffolding this service

If you scaffold from a Python service template (e.g. `python-template`'s `service` archetype in this
new repo):
- **FastAPI `create_app()`** app-factory with a fail-soft lifespan; `/healthz` + `/readyz`. Add your
  `/v1/converse` router here.
- **pydantic-settings `config.py`** — the settings in §5.2.
- **Multi-stage non-root `Dockerfile` + `compose.yml`** — you can run this next to HA in a stack.
- **Tests**: unit + a **contract test** asserting the SSE event shapes; integration against a mock
  (and eventually real) HA `mcp_server`.
- **MCP client, not server** — you are an MCP *client* to HA; you do not need to *expose* an MCP
  server. Add MCP-client deps (`mcp`, or `langchain-mcp-adapters` if you go the LangGraph route). If
  your template has an "include MCP server" toggle, leave it off.

---

## 8. Security

- **`HA_TOKEN` is a powerful secret** (full control of exposed entities via MCP). Keep it in `.env` /
  a secrets file + pydantic-settings; **never commit it** (commit only `*.example`).
- **Shim ⇄ agent auth**: a shared bearer (`SHIM_AUTH_TOKEN`). Reject unauthenticated `/v1/converse`
  calls.
- **Network**: if this runs off the HA box, use TLS and restrict who can reach `/v1/converse`.
- Treat incoming `text` as **untrusted user input**, and LLM/tool output as data.

---

## 9. Suggested build order (each phase shippable/testable)

1. **Contract skeleton** — pydantic settings + `POST /v1/converse` returning a *stubbed* SSE stream
   (echo the input as `text_delta`s + `done`). Wire `SHIM_AUTH_TOKEN`. Contract test on the SSE
   shapes. *(No LLM yet — the shim can already talk to this.)*
2. **LLM provider (OpenAI-compatible first)** — streaming chat, non-agentic: text in → streamed text
   out. Configurable base URL. Test against a mocked backend. Then add **Anthropic**. (`llm-providers.md`)
3. **MCP client → HA** — connect, list tools, call a tool. Integration test against a real or mocked
   HA `mcp_server`. (`mcp-to-home-assistant.md`)
4. **Agent loop** — wire LLM tool-calls ↔ HA MCP tools, bounded iterations, stream deltas. The core
   voice turn end-to-end.
5. **Session / memory** keyed by `conversation_id`.
6. **(Optional) LangGraph deep-agent path** for autonomous/non-voice tasks — off the voice hot path.
7. **Harden** — timeouts, `error`/fallback semantics, observability, Docker/compose deploy.

---

## 10. Open decisions for the owner

1. **Transport**: SSE-over-HTTP *(fixed for v1 — the shim implements it)* vs WebSocket *(future)*.
2. **Control channel**: HA `mcp_server` *(recommended)* vs HA REST/WebSocket API *(fallback)*. See
   `mcp-to-home-assistant.md`.
3. **LLM client**: hand-rolled provider adapters (inlined in `llm-providers.md`) vs official
   `openai`/`anthropic` SDKs vs LangChain wrappers.
4. **LangGraph deep agents**: build now (autonomous capability) or start with a simple loop and add
   later.
5. **Backend matrix**: which of llama-swap / Ollama / vLLM / LiteLLM / Anthropic are first-class, and
   which reliably support tool-calling + structured output.
6. **Memory**: per-session only vs cross-restart persistence (drives a checkpointer/store).
7. **CI runner / Python floor** for this repo.

---

## 11. Verify against a running HA 2026.7 before relying on it

1. **HA `mcp_server` endpoint + auth** for your HA version — see the VERIFY list in
   `mcp-to-home-assistant.md`.
2. **End-to-end early-TTS latency** of streaming your deltas through the Assist pipeline — a real
   voice test, not just unit tests.
3. **Which local OpenAI-compatible backends** honor tool-calling / structured output reliably vs
   silently ignore it (probe each target backend — see `llm-providers.md`).
4. **`continue_conversation`** behavior through the shim → pipeline for multi-turn voice.
