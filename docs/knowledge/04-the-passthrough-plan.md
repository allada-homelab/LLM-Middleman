# 04 — The Passthrough Plan (the original shim idea, end-to-end)

This is the detailed plan we arrived at for the passthrough idea — the "HA Voice front-end + external
agent brain" design — captured end-to-end so a from-scratch build has the whole picture in one place.
The middleman-service specifics are expanded in `docs/plans/middleman-implementation-brief.md`; this
doc is the **system-level** plan spanning both halves.

---

## 1. The idea in one paragraph

Use Home Assistant's excellent voice front-end (mics / Voice satellites / VPE hardware, wake word,
STT, TTS) as-is, but replace the "brain" with an **external agent service**. A thin HA
**conversation-agent shim** plugs into the Assist pipeline and *forwards* each recognized utterance
to the **middleman** service; the middleman runs an LLM agent loop, **controls the home by calling
back into HA over MCP**, and streams the reply text back to the shim → TTS. This keeps heavy agent
dependencies out of HA, and is the natural (and only clean) place to run something like LangGraph
deep agents.

---

## 2. End-to-end flow

```
 1. User speaks           → HA mic / satellite
 2. Wake word + STT       → HA Assist pipeline
 3. Intent stage          → local sentence-triggers/intents first (timers, simple commands)…
                            …else → the conversation agent = SHIM
 4. Shim                  → POST /v1/converse {conversation_id, text, …}  (SSE response)
 5. Middleman             → runs agent loop over an LLM (OpenAI-compatible / Anthropic)
 6.   tool call needed    → Middleman (MCP client) → HA mcp_server → executes HA service → result
 7.   assistant text      → streamed back as SSE text_delta events …
 8. Shim                  → converts text_delta → AssistantContentDeltaDict → ChatLog
 9. HA pipeline           → streams to TTS (starts speaking after ~60 chars)
10. Middleman             → `done` event (final text, continue_conversation?)
```

Two integration points with HA, both stock:
- **Conversation agent** (the shim) — voice *in/out*. Custom (this project).
- **`mcp_server`** — the middleman reaches *back* for home control. Stock HA integration; **zero
  custom HA code** to expose tools.

---

## 3. Component responsibilities

| Component | Owns | Explicitly does NOT own |
|---|---|---|
| **HA Assist pipeline** (stock) | wake word, STT, TTS, intent routing, `conversation_id`, session TTL | the agent brain |
| **Shim** (custom HA integration) | forwarding the turn, streaming deltas back, fallback, config | LLM calls, tools, agent loop |
| **Middleman** (FastAPI service) | agent loop, LLM providers, MCP client → HA, streaming, memory | voice I/O, HA session lifecycle |
| **HA `mcp_server`** (stock) | exposing Assist tools (exposed entities) to the middleman over SSE | anything custom |

---

## 4. Control path: how the middleman actually changes the home

Preferred: the middleman is an **MCP client** to HA's built-in **`mcp_server`**, which exposes HA's
Assist LLM API (intents + entities *exposed to Assist*) as tools over SSE with bearer-token auth. The
middleman lists those tools, and when the LLM asks to call one, executes it via MCP and feeds the
result back into the loop.

- **Control surface = only entities exposed to Assist.** Document this for the owner; it's the safety
  boundary.
- **Alternative** if MCP is awkward: HA REST (`POST /api/services/...`, `GET /api/states`) or the
  WebSocket API with a long-lived token. More manual; no MCP dependency.
- **Verify live:** the exact `mcp_server` SSE endpoint + auth mechanism for the pinned HA version
  (see `06` verification list).

---

## 5. Phased build order (system-level)

Each phase is independently testable.

1. **Contract skeleton (middleman).** pydantic settings + `POST /v1/converse` returning a *stubbed*
   SSE stream (echo input as `text_delta`s + `done`). Wire the shared-secret auth. Contract test on
   the SSE event shapes. *(No LLM yet.)*
2. **Shim skeleton (HA).** Minimal `ConversationEntity` that forwards to the stubbed middleman and
   streams the echo back into `ChatLog`. Prove end-to-end text round-trips through Assist (typed
   input first, then voice).
3. **LLM provider (middleman).** OpenAI-compatible streaming chat, non-agentic: text in → streamed
   text out. Then Anthropic. Test against a mocked backend.
4. **MCP client → HA (middleman).** Connect to `mcp_server`, list + call a tool. Integration test
   against a real/mock HA.
5. **Agent loop (middleman).** Wire LLM tool-calls ↔ HA MCP tools, bounded iterations, stream deltas.
   This is the first *real* voice turn end-to-end.
6. **Session/memory (middleman).** Keyed by `conversation_id`; bounded. Persistent store only if
   cross-restart memory is wanted.
7. **Hardening.** Timeouts, `error`/fallback semantics on both sides, observability (LLM I/O + tool +
   latency logs), Docker/compose deploy next to HA.
8. **(Optional) Deep-agent path.** LangGraph/deepagents for autonomous, non-voice, long-horizon jobs
   — kept off the voice hot path.

---

## 6. Where each half is built

- **Middleman** → this repo (`LLM-Middleman`, python-template `service` scaffold, already created).
- **Shim** → a HACS-structured home (own repo, or folded into the `LLM-Home-Controller` rewrite as a
  passthrough agent type). **Cannot** share the middleman's service repo (HACS one-integration-per-repo;
  and a FastAPI service isn't a HA integration). See `03` §2 and `05`.

---

## 7. What could go wrong (risks we identified)

- **Voice latency.** A network hop + tool round-trips + a multi-step agent = dead air. Mitigate with
  streaming, shallow voice turns, an early preamble, and routing long-horizon work off the voice path.
- **Reliability coupling.** HA voice is degraded when the middleman is down. The shim must fall back;
  the middleman must degrade `/readyz` fast.
- **Security blast radius.** The middleman holds an HA long-lived token = full control of exposed
  entities. Treat it as a top-tier secret; auth the shim⇄middleman channel; TLS + ACLs if off-box.
- **Two moving parts, one contract.** The shim and middleman must agree on the SSE contract (§4 of
  `03`). Keep it in one place, versioned (`/v1/`).

---

## 8. Relationship to the sibling repo

The prior `LLM-Home-Controller` is an *in-HA* agent (LLM runs inside HA). The passthrough is a
different topology (LLM runs *outside* HA), but it **reuses**:
- the HA plumbing knowledge (ConversationEntity/ChatLog/streaming/Assist) — see `01` and `03`;
- the provider/LLM patterns (adapters, streaming, tool-calling, small-model hardening, structured
  output) — see `02` and `01` §5;
- the hard-won correctness lessons — see `01` §7 and `05` §"Lessons carried forward".
