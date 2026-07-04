# 05 — Architecture Decisions & Tradeoffs (the decision log)

Every consequential decision we reached, why, the alternatives weighed, and the pros/cons — so a
from-scratch build doesn't re-litigate settled forks or lose the reasoning behind them. Items marked
**(open)** are genuinely the owner's call and not yet decided.

---

## Decision 1 — Passthrough (external brain) vs. embedding the agent inside HA

**Chosen: passthrough.** The agent brain lives in an external service; HA runs only a thin shim.

**Why.** Embedding a heavy agent framework *inside* HA fights HA's `ChatLog` (which wants to own the
tool loop) and bloats HA's pinned dependency tree (hassfest/quality-scale friction, version
conflicts). A passthrough keeps HA's strong voice front-end, isolates the brain's dependencies and
release cadence, and is the natural home for LangGraph/deep agents.

| | Passthrough (chosen) | Embed in HA |
|---|---|---|
| HA dep footprint | tiny (aiohttp) | large; conflict/bloat risk |
| Uses HA's `ChatLog` loop | no (shim just forwards) | yes (must conform) |
| Agent framework freedom | full (own container) | constrained by HA env |
| Voice latency | +1 network hop | in-process |
| Moving parts | two (+contract) | one |
| Reliability | HA voice depends on service | self-contained |

**Trade accepted:** an extra network hop + a second deployable, in exchange for isolation and
freedom. The latency cost is mitigated by mandatory streaming (Decision 5).

---

## Decision 2 — LangGraph / deep agents: where (if at all)

**Chosen: not inside HA; optionally in the external middleman, primarily for autonomous work.**

**Why.** LangGraph's value (checkpointer memory, planner + sub-agents = "deep agents") is real but
its dependency tree and long-horizon loop fight HA's grain and hurt voice latency. Its
uniquely-good capability (long-horizon autonomy) isn't needed for a 1–3-tool voice turn. The
passthrough makes this a non-dilemma: run it in the middleman container if/when you want autonomy.

- **Voice/conversation turns:** don't use deep agents — shallow, latency-sensitive.
- **Autonomous / AI-Task background jobs:** deep agents genuinely fit (no human waiting).

**Alternatives weighed:** LangGraph inside HA (rejected: deps + fights `ChatLog`); no LangGraph at
all (fine for v1 — a simple bounded tool loop covers voice). **The "built-in Anthropic/OpenAI
support" LangGraph offers is the part HA/our provider layer already solves well; the part LangGraph
is uniquely good at (autonomy) is a capability not yet required — so adopt it only when that
capability is the goal.**

---

## Decision 3 — Home control channel: HA `mcp_server` (MCP) vs REST/WS

**Chosen (recommended): HA's built-in `mcp_server`; the middleman is an MCP client.**

**Why.** `mcp_server` already exposes HA's Assist tools (exposed entities) over SSE with bearer auth
— the middleman gets the home-control surface **for free**, no custom HA tool-bridging. Clean
separation.

- **Pro:** zero custom HA code for tools; safety boundary = "exposed to Assist"; standard protocol.
- **Con:** a tool round-trip back into HA per action (latency); needs a long-lived HA token.
- **Alternative:** HA REST/WebSocket API — more manual, no MCP dep. Keep as fallback.
- **(verify live)** exact endpoint + auth for the pinned HA version.

---

## Decision 4 — Two repos / one integration per repo (HACS reality)

**Chosen: the shim and the middleman are separate deliverables; the shim needs a HACS-structured
home, not this service repo.**

**Why.** **HACS enforces exactly one integration per repository** (verbatim: *"There must only be one
integration per repository … only the first one will be managed"*). And a HA integration must be a
`custom_components/<domain>/` package — a FastAPI service cannot be one. So:

- Options for the shim's home: **(a) fold it into the `LLM-Home-Controller` rewrite as a new
  passthrough *agent type*** (one install, one repo — **recommended** if it's "one product") **or
  (b) a dedicated HACS repo** (clean independence, two installs).
- A two-domain monorepo is **not** viable for HACS (only the first domain is managed) — works only
  for manual installs.

**Product-identity is the deciding factor**, not mechanics: "one product with a passthrough mode" →
(a); "two independent tools" → (b).

---

## Decision 5 — Streaming: mandatory

**Chosen: `_attr_supports_streaming = True`, emit assistant text early; the middleman streams
`text_delta`s.**

**Why.** It is the primary latency lever for agentic voice — HA feeds TTS after ~60 accumulated
characters, so the assistant can speak before the tool loop finishes. Without it, a multi-tool turn
is dead air. **Known gap:** when the first useful text *requires* a tool, streaming can't help and
there's no first-class "filler" primitive (2026.7) — mitigate with an early preamble + shallow voice
turns.

---

## Decision 6 — Transport for shim ⇄ middleman **(recommended, semi-open)**

**Recommended: HTTP POST with an SSE response stream.** Matches HA's streaming idiom, trivial from
`aiohttp`, one-directional is enough. **Alternative:** WebSocket (only if you later need mid-turn
bidirectional signalling). Reversible; start with SSE.

---

## Decision 7 — Iteration caps split by surface

**Chosen: separate caps — voice/interactive shallow (~3–10), autonomous/AI-Task deep (~1000).** One
constant is wrong: a human waiting at a mic needs a short loop; a background job doesn't.

---

## Decision 8 — LLM client for the middleman **(open)**

**Options:** (a) port the prior repo's `Protocol`/adapter code (proven, minimal deps, full control);
(b) official `openai`/`anthropic` SDKs (less code, two SDKs' churn); (c) LangChain wrappers (most
helpers, heaviest deps — only if going the deep-agent route). **Lean:** (a) or (b) for v1; (c) only
when Decision 2's autonomy capability is the goal.

---

## Decision 9 — No `DataUpdateCoordinator`; no in-integration MCP server/client (HA side)

**Chosen: don't add either to the shim.** A conversation agent is stateless per-turn (nothing to
poll → no coordinator). MCP is already provided by stock `mcp`/`mcp_server` — building it into an
integration reimplements stock code for no gain.

---

## Open decisions still needing the owner's call

1. **(D6)** SSE vs WebSocket transport (lean SSE).
2. **(D8)** LLM client: ported adapters vs official SDKs vs LangChain.
3. **(D2)** Deep agents now (autonomous capability) or later.
4. **(D4)** Shim's home: fold into `LLM-Home-Controller` vs new HACS repo.
5. **Backend matrix:** which of llama-swap / Ollama / vLLM / LiteLLM / Anthropic are first-class; which
   reliably support tool-calling + structured output.
6. **Memory:** per-session only vs cross-restart persistence.
7. **HA-side tool exposure:** pure passthrough (all tools via MCP) vs. shim *also* exposing Assist
   tools locally.
8. **Repo restructure:** if building the *shim* in `LLM-Middleman`, it must move to a HACS
   `custom_components/` layout (the current scaffold is a FastAPI service, i.e. the *middleman*).

---

## Lessons carried forward (from the prior `LLM-Home-Controller` review — `01` §7)

These are correctness/design lessons that apply regardless of topology:

- **Never gate custom tools on a nullable dependency** (the prior repo silently dropped tools when no
  HASS API was selected). Give tools an independent registration path.
- **Don't string-splice a framework-owned prompt** — own your prompt via a proper extension point.
- **Don't claim durability the code doesn't deliver** — the prior "memory" was an in-RAM buffer that
  died on restart despite a persistent-sounding name. Persist it or name it honestly.
- **No blocking I/O on the event loop** (`read_bytes()` in async) — use executors. (HA-side lesson;
  in FastAPI, use async I/O / thread offload equally.)
- **Preserve opaque provider continuation state** (Anthropic thinking `signature` / `native`) — don't
  discard it if you replay reasoning.
- **Real tokenizer or message-count trimming**, not `len(text)//4`.
- **Surface backend errors** (don't swallow `get_models` failures into `[]`).
- **Factor shared code** (get_models, attachment encoding) instead of per-adapter copies.
