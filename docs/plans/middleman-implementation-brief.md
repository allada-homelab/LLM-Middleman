# LLM Middleman — Implementation Brief

> **Audience:** the Claude Code session that will implement this service.
> **Status:** scaffold created (python-template `service` archetype, gate green). No middleman
> logic written yet. This document is the full context + contract you need to build it.
> **Sibling repo:** `LLM-Home-Controller` (the Home Assistant custom integration being rewritten).
> Deeper HA research lives there at `docs/research/ha-2026.7-rewrite-research.md` and `research.md`
> — but this brief is written to be **self-contained** on everything you need.

---

## 0. Read this first — scope & interpretation (CONFIRM WITH OWNER)

This division of responsibilities was **inferred** from the design discussion. If any of it is
wrong, stop and confirm before building — it shapes everything:

- **This repo (`LLM-Middleman`) = the external "brain" service.** A FastAPI service that receives a
  recognized voice/text turn from Home Assistant, runs an LLM agent loop, controls the home, and
  streams a reply back. This is what you build.
- **This repo is NOT a Home Assistant integration.** HA custom integrations live in a HACS repo
  under `custom_components/<domain>/`; a FastAPI service cannot be one. **HACS enforces exactly one
  integration per repository** (verbatim from the HACS docs: *"There must only be one integration
  per repository … If there are more than one, only the first one will be managed."*). So the HA
  side cannot live here.
- **The HA-side "shim" is a separate deliverable** (a thin `ConversationEntity`), expected to be
  folded into the `LLM-Home-Controller` rewrite as a new *passthrough agent type*. You do **not**
  build it here — but this brief **defines the contract** between it and this service so both sides
  match.

---

## 1. The architecture (the "why")

```
┌─────────────────────────── Home Assistant (owns the voice front-end) ───────────────────────────┐
│                                                                                                  │
│   HA mic / Voice satellite / VPE                                                                  │
│        │  wake word → STT                                                                         │
│        ▼                                                                                          │
│   Assist pipeline ──► Conversation agent = **HA shim** (thin ConversationEntity, separate repo)   │
│        ▲                    │  forwards the utterance (+ conversation_id)                          │
│        │  TTS ◄── streamed  │                                                                      │
│        │  text deltas       ▼                                                                      │
│        │              ┌───────────────── HTTP/SSE contract (§4) ─────────────────┐                │
│        │              │                                                          │                │
│   HA `mcp_server` ◄───┼──── MCP client (control the home) ◄──┐                   │                │
│   (Assist tools       │                                      │                   ▼                │
│    over SSE)          │                            ┌─────────────────── LLM Middleman (THIS repo) ─┐
│                       └──── streamed reply ◄────────┤  FastAPI service                             │
│                                                     │   • agent loop over an LLM                   │
└─────────────────────────────────────────────────────┤   • OpenAI-compatible + Anthropic providers │
                                                       │   • MCP client → HA tools                   │
                                                       │   • (optional) LangGraph deep agents        │
                                                       │   • session/memory keyed by conversation_id │
                                                       └──────────────────────────────────────────┘
```

**Why passthrough instead of an in-HA agent?** Putting a heavy agent framework (e.g. LangGraph /
deepagents) *inside* HA's Python environment fights HA's `ChatLog` (which wants to own the tool
loop) and bloats HA's pinned dependency tree. A passthrough keeps HA's excellent voice front-end,
keeps the brain's dependencies in their own container/release cadence, and lets you use LangGraph
**outside** HA where it doesn't fight anything. HA already ships both halves you need: a pluggable
`ConversationEntity` (voice in/out) and `mcp_server` (external agent reaches back for control).

**Design decisions already made (context, not up for re-litigation unless owner reopens):**
- Passthrough over embedding an agent framework in HA. ✔
- Home control via HA's built-in `mcp_server` (MCP client here) is the preferred control channel. ✔
- Streaming is **mandatory** for acceptable voice latency (early TTS). ✔
- LangGraph deep agents, *if used*, belong here (outside HA) and primarily for **autonomous /
  non-voice** multi-step work — not the latency-sensitive voice hot path. ✔

---

## 2. Home Assistant facts you need (self-contained)

You don't need HA internals to build the service, but you need to understand the two integration
points.

### 2.1 The Assist voice pipeline
`wake word → STT → conversation agent → TTS`. The conversation agent is a pluggable entity. Our HA
shim *is* that agent; instead of calling an LLM itself, it forwards the recognized text to this
service and streams the reply back into TTS.

Key HA behaviors the contract must respect:
- **`conversation_id`** — HA assigns/continues a conversation id across turns in a session. It is the
  session key. This service maintains its own per-`conversation_id` state (history/memory).
- **Streaming** — the HA shim sets `_attr_supports_streaming = True` and drives
  `chat_log.async_add_delta_content_stream()`. To make TTS start speaking on the *first sentence*
  rather than the last, **this service must stream assistant text deltas** as they are produced. The
  shim converts our `text_delta` events into HA `AssistantContentDeltaDict` deltas.
- **`continue_conversation`** — HA supports keeping the mic open for a follow-up. If the agent wants
  a follow-up turn (e.g. asked a clarifying question), it signals this in the final event.

### 2.2 HA `mcp_server` (how this service controls the home)
HA's built-in **`mcp_server`** integration exposes HA's **Assist LLM API** (the same tools the
built-in agent uses — intents like turning devices on/off, plus the entities the user has **exposed
to Assist**) as an **MCP server over SSE**, authenticated with an HA **long-lived access token**
(bearer). This service connects as an **MCP client**, lists the tools, and calls them when the LLM
requests a tool.

- The control surface = **only entities exposed to Assist** in HA. Document this for the owner.
- **Verify live (see §11):** the exact SSE endpoint path and the precise auth mechanism
  (application-credentials OAuth vs plain long-lived token) for the pinned HA version.
- **Fallback control channel** if MCP proves awkward: HA's REST API (`POST /api/services/...`,
  `GET /api/states`) or WebSocket API with a long-lived token — more manual, no MCP dependency.

---

## 3. What "the agent" does per turn (behavioral spec)

1. Receive `{ conversation_id, text, language, ... }` from the shim.
2. Load/attach session state for `conversation_id` (prior turns / memory).
3. Run a **bounded tool-calling loop** against an LLM:
   - Tools = HA tools discovered via the MCP client (+ any local tools you add).
   - The LLM may call tools (control the home / query state); execute them via MCP; feed results
     back; continue until the LLM produces a final answer or the iteration cap is hit.
   - **Voice turns should be shallow** (small max-iterations, e.g. 3–5) — a human is waiting.
4. **Stream** assistant text deltas back as they are generated (before the whole answer is done).
5. Emit a final event (full text, optional `continue_conversation`).
6. Persist updated session state.

Autonomous / non-voice jobs (if in scope) can use a **much higher** iteration budget and optionally
a LangGraph deep-agent graph — but keep that path separate from the voice path.

---

## 4. The contract: HA shim ⇄ Middleman  (THE critical interface)

Both sides must agree on this. Recommended shape:

**Transport (recommended): HTTP POST with a Server-Sent Events (SSE) response stream.** SSE matches
HA's own streaming idiom, is trivial to consume from `aiohttp` in the shim, and is one-directional
(request → streamed response) which is all a turn needs. (WebSocket is the alternative if you later
need mid-turn bidirectional signalling; not needed for v1.)

**Endpoint:** `POST /v1/converse`  (auth: `Authorization: Bearer <shared secret>`, see §8)

**Request body (JSON):**
```jsonc
{
  "conversation_id": "01J...",      // HA session key; null on a brand-new conversation
  "text": "turn off the kitchen lights",
  "language": "en",
  "device_id": "…",                 // optional: originating satellite/device
  "context": {                      // optional: extra grounding the shim can supply
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

**Shim behavior (documented here so the service author knows the consumer):**
- Converts each `text_delta` → HA `AssistantContentDeltaDict` for early TTS.
- On `error` or timeout → falls back (canned message, or HA's default agent). The service should
  therefore **fail fast with a clear `error` event** rather than hang.
- Passes `conversation_id` through unchanged; treats `null` as "new session".

---

## 5. Service responsibilities (what to build here)

1. **API layer** — a router implementing `POST /v1/converse` (§4) on top of the scaffold's
   `create_app()`. Keep `/healthz` (always 200) and `/readyz` (report degraded when a backend/HA is
   unreachable) from the template.
2. **Config** (`src/llm_middleman/config.py`, pydantic-settings) — env-driven:
   - `HA_BASE_URL`, `HA_TOKEN` (long-lived), `HA_MCP_URL` (or derive from base url).
   - LLM providers: base URL(s) + API key(s) + model name(s) for **OpenAI-compatible** and
     **Anthropic**; primary/fallback model chain; temperature; `MAX_TOOL_ITERATIONS` (voice) and a
     separate (higher) budget for autonomous tasks.
   - `SHIM_AUTH_TOKEN` (shared secret the shim presents).
   - Timeouts (per-turn deadline, per-LLM-call, per-tool-call).
3. **LLM provider layer** — streaming chat with tool-calling for:
   - **OpenAI-compatible** (Chat Completions) — the primary target for llama-swap / Ollama / vLLM /
     LiteLLM. Configurable base URL.
   - **Anthropic** (Messages API) — streaming + optional extended thinking.
   - You may **crib the provider `Protocol`/adapter pattern** from `LLM-Home-Controller`'s
     `custom_components/llm_home_controller/providers/` — it cleanly isolates the incompatible wire
     formats and was flagged as that repo's single strongest asset. Or use the official `openai` /
     `anthropic` SDKs directly. **Decide in §10.**
4. **MCP client → HA** — connect to HA `mcp_server`, list tools, execute tool calls the LLM
   requests, thread results back. Libraries: the official **`mcp`** Python SDK, or
   **`langchain-mcp-adapters`** if you go the LangGraph route.
5. **Agent loop** — bounded tool-calling loop, streaming text deltas out; shallow for voice.
6. **Session / memory** — keyed by `conversation_id`; bounded. Simple in-process store for v1; a
   LangGraph checkpointer or a persistent store if cross-restart memory is wanted (owner decision).
7. **Observability** — structured logs of LLM I/O, tool calls, and per-stage latency (the template
   ships logging config). Latency visibility is critical for a voice UX.

---

## 6. Latency & UX requirements (non-negotiables for voice)

- **Stream text deltas** the moment the LLM emits them → early TTS. Do not buffer the whole answer.
- **Keep voice turns shallow.** Deep multi-step reasoning + a network hop + tool round-trips = a
  human waiting in silence. Route genuinely long-horizon work to a non-voice/AI-Task path.
- **Hard per-turn deadline** with a clear `error` event so the shim can fall back — never hang the
  pipeline.
- Consider an early **filler/ack** delta ("One moment…") if a turn will visibly stall on tool calls.
- **Reliability coupling:** when this service is down, HA voice is degraded. The shim falls back, but
  design for graceful failure and fast `/readyz` degradation.

---

## 7. What the scaffold already gives you (python-template `service` tier)

- **FastAPI `create_app()`** app-factory with a fail-soft lifespan; **`/healthz`** (always 200) and
  **`/readyz`** (degraded reporting). Add your `/v1/converse` router here.
- **pydantic-settings `config.py`** — extend with the settings in §5.2.
- **Multi-stage non-root `Dockerfile` + `compose.yml`** (+ `compose.override.yml`). You can add the
  middleman to a compose stack next to HA.
- **Tests**: unit (default), `tests/integration` (testcontainers), `tests/contract` tiers;
  `tests/test_app.py` smoke. **Coverage gate `fail_under=80`.**
- **CI**: `ci` / `audit` / `pre-commit`, `docker-publish` (GHCR, provenance+SBOM), `hadolint`,
  `image-scan` (Trivy). Runner defaults to **`homelab-runners`** — override per-repo/org with the
  `CI_RUNNER` variable (no file edits) if this repo is public / lacks those runners.
- **Toolchain**: uv + PEP 735 groups; ruff broad ruleset; **basedpyright strict**; pytest with
  `filterwarnings=["error"]` + `asyncio_mode=auto`. Python floor **3.11**, default **3.13**.
- **`include_mcp_server = false`** — we intentionally did **not** scaffold a FastMCP *server*. This
  service is an MCP **client** to HA, not a server. Add MCP-client deps yourself (`mcp` or
  `langchain-mcp-adapters`). If you later want to *expose* tools via MCP, re-render with the toggle
  or add FastMCP manually.

---

## 8. Security

- **`HA_TOKEN` is a powerful secret** (full HA control via MCP). Use the template's `secrets/` +
  `.env` + pydantic-settings; **never commit it** (`.env` and `secrets/*.txt` are gitignored;
  `*.example` files are the only committed ones).
- **Shim ⇄ middleman auth**: a shared bearer (`SHIM_AUTH_TOKEN`). Reject unauthenticated calls.
- **Network**: if the service runs off the HA box, use TLS and restrict who can reach `/v1/converse`.
- Treat all incoming `text` as **untrusted user input**, and treat LLM/tool output as data.

---

## 9. Suggested build order (phased, each phase shippable/testable)

1. **Contract skeleton** — pydantic settings + `POST /v1/converse` returning a stubbed SSE stream
   (echo the input as `text_delta`s + `done`). Wire `SHIM_AUTH_TOKEN`. Write a **contract test**
   asserting the SSE event shapes. *(No LLM yet.)*
2. **LLM provider (OpenAI-compatible first)** — streaming chat, non-agentic: text in → streamed text
   out. Configurable base URL. Test against a mocked backend. Then add **Anthropic**.
3. **MCP client → HA** — connect, list tools, call a tool. Integration test against a real or mocked
   HA `mcp_server`.
4. **Agent loop** — wire LLM tool-calls ↔ HA MCP tools, bounded iterations, stream deltas. This is
   the core voice turn end-to-end.
5. **Session / memory** keyed by `conversation_id`.
6. **(Optional) LangGraph deep-agent path** for autonomous/non-voice tasks — kept off the voice hot
   path.
7. **Harden** — timeouts, `error`/fallback semantics, observability, Docker/compose deploy.

In parallel (separate repo): the HA **shim** is built to the §4 contract.

---

## 10. Open decisions for the owner (resolve before / during build)

1. **Transport**: SSE-over-HTTP *(recommended)* vs WebSocket.
2. **Control channel**: HA `mcp_server` *(recommended)* vs HA REST/WebSocket API.
3. **LLM client**: reuse `LLM-Home-Controller`'s provider adapters vs official `openai`/`anthropic`
   SDKs vs LangChain wrappers.
4. **LangGraph deep agents**: build now (autonomous capability) or start with a simple loop and add
   later.
5. **Backend matrix**: which of llama-swap / Ollama / vLLM / LiteLLM / Anthropic are first-class,
   and which reliably support tool-calling + structured output.
6. **Memory**: per-session only vs cross-restart persistence (drives a checkpointer/store).
7. **Where the HA shim lives**: fold into the `LLM-Home-Controller` rewrite as a *passthrough agent
   type* *(recommended, given HACS one-integration-per-repo)* vs a new HACS repo.
8. **CI runner** (`homelab-runners` vs `ubuntu-latest`) and **Python floor** (keep 3.11 vs bump).

---

## 11. Least confident / verify against a running HA 2026.7 before relying on it

1. **HA `mcp_server` exact SSE endpoint path + auth mechanism** (application-credentials OAuth vs
   plain long-lived token) for the pinned HA version. Load-bearing for §2.2 and the MCP client.
2. **End-to-end early-TTS latency** of streaming a custom agent's deltas through the Assist pipeline
   — confirm with a real voice test, not just unit tests.
3. **Streaming-TTS maturity** in 2026.7 (does TTS actually start on the first sentence?).
4. **Which local OpenAI-compatible backends** honor tool-calling / structured output reliably vs
   silently ignore it (probe each target backend).
5. **`continue_conversation` plumbing** through the shim → pipeline for multi-turn voice.

---

## 12. Known scaffold follow-ups

- **`.copier-answers.yml` `_src_path`** currently points at the local template path
  (`/home/vscode/LLM-Middleman`) and `_commit: 4aa4927`. For `copier update` to pull future
  python-template improvements, repoint it at the canonical upstream
  (`gh:allada-homelab/python-template`) and align `_commit` to a real upstream tag.
- **`include_mcp_server` was left `false`** by design (we're an MCP client). Revisit only if you
  decide to *expose* MCP tools from this service.

---

## 13. References

**Home Assistant developer docs**
- Conversation entity — https://developers.home-assistant.io/docs/core/entity/conversation/
- LLM API (tools) — https://developers.home-assistant.io/docs/core/llm/
- `conversation/chat_log.py` (streaming delta contract) —
  https://github.com/home-assistant/core/blob/dev/homeassistant/components/conversation/chat_log.py
- Assist pipelines — https://developers.home-assistant.io/docs/voice/pipelines/
- Assist satellite entity — https://developers.home-assistant.io/docs/core/entity/assist-satellite/
- AI Task entity (for the autonomous path) — https://developers.home-assistant.io/docs/core/entity/ai-task/

**Home Assistant user docs**
- `mcp_server` (HA as MCP server) — https://www.home-assistant.io/integrations/mcp_server/
- `mcp` (HA as MCP client) — https://www.home-assistant.io/integrations/mcp/
- TTS streaming discussion — https://github.com/orgs/home-assistant/discussions/2277

**Sibling repo (deeper research + provider prior-art)**
- `LLM-Home-Controller/docs/research/ha-2026.7-rewrite-research.md` — full HA 2026.7 best-practices
  research (conversation stack, llm helpers, mcp, reference integrations).
- `LLM-Home-Controller/custom_components/llm_home_controller/providers/` — working OpenAI /
  OpenAI-Responses / Anthropic adapter pattern to crib.
