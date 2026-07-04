# LLM Provider Layer

The provider layer for your external agent — talking to LLM backends for agentic, multi-tool control.
Self-contained. (Prior art, reference only: the `LLM-Home-Controller` repo's `providers/` package is a
working implementation of the pattern below — you don't need it on disk.)

---

## 1. The backends to target

Built for **OpenAI-compatible** self-hosted backends plus **Anthropic**:

| Backend | Wire API | Notes |
|---|---|---|
| **llama-swap** | OpenAI Chat Completions | model-swapping proxy in front of llama.cpp; primary homelab target |
| **Ollama** (OpenAI mode) | OpenAI Chat Completions (`/v1`) | also has a native API; the OpenAI shim is the portable path |
| **vLLM** | OpenAI Chat Completions | high-throughput; good tool-calling on capable models |
| **LiteLLM** | OpenAI Chat Completions | proxy/gateway fanning out to many providers behind one URL |
| **Anthropic** | Messages API | Claude; extended thinking + native structured output |
| (optional) **real OpenAI** | **Responses API** | different endpoint/shape — a *separate* adapter, not the base |

**Design rule:** the OpenAI-compatible leg is the structural default (Chat Completions: flat message
list + `tools` + `stream=True`). Do **not** model on OpenAI's stateful **Responses API** — it's the
wrong shape for llama-swap/vLLM/LiteLLM. Every backend base URL must be **configurable** so
retargeting = a setting change, not code.

---

## 2. The provider abstraction (the pattern to reuse)

Isolate the incompatible wire formats behind a thin `Protocol`/adapter seam (~7 methods). Capability
flags live **on** the provider, not duplicated in config conditionals:

```python
class LLMProvider(Protocol):
    supports_thinking: bool
    supports_native_structured_output: bool

    def build_url(self, api_url: str) -> str: ...
    async def get_models(self, session, api_url, api_key) -> list[str]: ...
    def convert_content(self, messages) -> list[dict]: ...        # your history -> wire messages
    def build_payload(self, messages, tools, *, stream, extra) -> dict: ...
    async def stream(self, session, url, payload) -> AsyncIterator[bytes]: ...
    def transform_stream(self, raw) -> AsyncIterator[Delta]: ...  # wire SSE -> normalized deltas
```

**Lessons baked in (avoid these — all seen in the prior implementation):**
- Factor a shared `base.py` helper for `get_models` / attachment→base64 — don't reimplement per
  adapter.
- Use relative imports inside the package; no absolute `custom_components…`-style imports.
- **Don't** swallow `get_models` errors into `[]` — that masks a bad API key as "no models". Surface
  the real error to the config/validation layer.
- Keep capability flags on the provider class; read them (don't duplicate in config conditionals).

---

## 3. Streaming (mandatory)

Streaming is the **primary latency lever** for voice (HA starts TTS after ~60 chars). All adapters
stream (`stream=True`) and emit normalized text/thinking/tool deltas as they arrive.

- **OpenAI-compatible SSE**: `data: {json}\n\n` lines, `[DONE]` sentinel. Deltas under
  `choices[0].delta` (`content`; `tool_calls` with incremental `arguments` fragments).
- **Anthropic**: typed event stream — `message_start`, `content_block_start`, `content_block_delta`
  (`text_delta` / `thinking_delta` / `input_json_delta`), `signature_delta`, `content_block_stop`,
  `message_delta`, `message_stop`.
- **Tool-call arguments stream as string fragments** — concatenate, then parse; often invalid until
  the final fragment. Budget a repair pass (§5).
- Treat SSE bytes as **untrusted wire data** (good fuzz target).
- **Provide a non-streaming fallback** for proxies that don't do SSE cleanly. Decide per backend.

---

## 4. Tool calling (the agent loop primitive)

You own the loop (unlike the in-HA case where `ChatLog` owns it):

1. Send messages + tool schemas to the LLM (`stream=True`). Tool schemas come from HA via the MCP
   client — see `mcp-to-home-assistant.md` §6 for the MCP→OpenAI/Anthropic mapping.
2. Accumulate streamed assistant text (emit it onward immediately for early TTS) and any `tool_calls`.
3. When a tool call completes, execute it via the HA MCP client, append the result as a tool message,
   loop.
4. Stop when the model returns a final answer with no new tool calls, or the **iteration cap** is hit.

**Iteration caps differ by surface:**
- **Voice / interactive**: shallow, ~**3–10**. A human is waiting; dead air kills UX.
- **Autonomous / background jobs**: deep, up to ~**1000**. No human waiting.

Split them; don't use one constant.

---

## 5. Small / open-weight model hardening

Open-weight models behind llama-swap/Ollama fail in specific, recurring ways. Ship these fixes
(HA's `ollama` integration does):

- **Fix invalid tool arguments** — models emit args as a *stringified* JSON blob, or with trailing
  junk; detect and re-parse.
- **Drop empty/`None` argument keys** the model hallucinates.
- **Trim history** — keep the system message + last *N* turn pairs to stay under the context window.
  Prefer **message-count trimming** over a `len(text)//4` token estimate (imprecise — over-prunes or
  overflows).

---

## 6. Structured output (two-tier — support is heterogeneous)

Local OpenAI-compatible backends' `response_format: json_schema` support is **inconsistent and
unadvertised** — some honor it, some silently ignore it; even when accepted, small models emit
non-conforming JSON. Strategy:

1. **Native mode** — pass `response_format`/`json_schema` (OpenAI-compatible) or Anthropic's
   structured-output config **when the model/backend is known to support it**.
2. **Forced-tool fallback** — otherwise, expose a single synthetic tool whose parameters *are* the
   schema, and force the model to call it.
3. **Always validate** the result (JSON-parse + schema check) and fail loudly on non-conformance —
   never trust the model honored the schema.

OpenAI "strict" mode wants `additionalProperties: false` + all-required; local backends differ —
verify per target.

---

## 7. Anthropic specifics (extended thinking + tool use)

- Claude's **extended thinking** blocks carry an opaque **`signature`** (and sometimes encrypted
  payload). To continue a thinking + tool-use conversation, **replay the thinking block *with* its
  signature** on the next turn — not just the visible text. If you can't preserve the signature, drop
  the thinking block entirely rather than replay it unsigned. (The prior implementation *discarded*
  the signature — a likely correctness bug; **verify against current Anthropic docs**.)
- Keep your own "opaque provider continuation" field to round-trip that state.
- Anthropic also supports **prompt caching** and **native structured output** with a forced-tool
  fallback — worth adopting.

---

## 8. Resilience patterns

- **Retry + sticky model-fallback:** distinguish **retryable** (429, 5xx, timeout — honor
  `Retry-After`) from **terminal** (400/401/404 — never retry, never fail over on auth). Keep the
  fallback model **sticky** across tool iterations within a turn so you don't flip-flop mid-turn.
- **Fail fast, loudly** on terminal errors so the shim (via `/v1/converse`) can fall back rather than
  hang.
- **Usage/token accounting** captured from the stream for observability.

---

## 9. Client choice (see brief §10)

Three ways to talk to the LLMs:
1. **Hand-rolled `Protocol`/adapter code** (§2) — minimal deps, you own every byte. Proven pattern.
2. **Official SDKs** (`openai`, `anthropic`) — less code, two SDKs + their churn.
3. **LangChain/LangGraph model wrappers** — most helpers (checkpointer memory, deep-agent graph),
   heaviest dependency tree; justified mainly if you want the deep-agent capability.

For v1, (1) or (2). Reach for (3) only when long-horizon autonomy is the actual goal.
