# 02 — LLM Backends & Provider Layer

> **Applies to the EXTERNAL agent, not the shim.** `LLM-Middleman` (the shim) has **no LLM client** —
> it forwards text to the external agent, which is what talks to the backends below. Read this when
> building that external agent (see `../plans/middleman-implementation-brief.md`), not the shim.

Everything we learned about talking to LLM backends for agentic, multi-tool control —
distilled to what the external agent needs. Deeper HA-side context is in
`01-home-assistant-reference.md` §4–§5; the *prior* implementation's provider code (a working,
reviewed reference) lives in the sibling repo at
`LLM-Home-Controller/custom_components/llm_home_controller/providers/`.

---

## 1. The backends we target

The system is built for **OpenAI-compatible** self-hosted backends plus **Anthropic**:

| Backend | Wire API | Notes |
|---|---|---|
| **llama-swap** | OpenAI Chat Completions | model-swapping proxy in front of llama.cpp; primary homelab target |
| **Ollama** (OpenAI mode) | OpenAI Chat Completions (`/v1`) | also has a native API; the OpenAI shim is the portable path |
| **vLLM** | OpenAI Chat Completions | high-throughput server; good tool-calling on capable models |
| **LiteLLM** | OpenAI Chat Completions | proxy/gateway that can fan out to many providers behind one URL |
| **Anthropic** | Messages API | Claude; extended thinking + native structured output |
| (optional) **real OpenAI** | **Responses API** | different endpoint/shape — keep as a *separate* adapter, not the base |

**Design rule:** the OpenAI-compatible leg is the structural default. Model the primary adapter on
HA core's **`ollama/entity.py`** (flat message list + `tools` + `stream=True`), *not*
`openai_conversation` (which targets the stateful **Responses API** and is the wrong shape for
llama-swap/vLLM/LiteLLM). Every backend base URL must be **configurable** (`build_url(api_url)`) so
retargeting = changing one setting, not code.

---

## 2. The provider abstraction (the pattern to reuse)

The prior repo's single strongest asset was a thin **`Protocol`/adapter seam** isolating the three
genuinely incompatible wire formats behind a ~7-method interface. Reuse this shape:

```python
class LLMProvider(Protocol):
    # capability flags live ON the provider (don't duplicate them in config conditionals)
    supports_thinking: bool
    supports_native_structured_output: bool

    def build_url(self, api_url: str) -> str: ...
    async def get_models(self, session, api_url, api_key) -> list[str]: ...
    def convert_content(self, messages) -> list[dict]: ...        # our history -> wire messages
    def build_payload(self, messages, tools, *, stream, extra) -> dict: ...
    async def stream(self, session, url, payload) -> AsyncIterator[bytes]: ...
    def transform_stream(self, raw) -> AsyncIterator[Delta]: ...  # SSE -> normalized deltas
```

**Lessons baked in from the prior review (avoid these):**
- **Don't** reimplement `get_models` / attachment→base64 per adapter — factor a shared `base.py`
  helper. (Prior repo had `get_models` byte-identical across two adapters; base64 3×.)
- **Don't** use absolute `custom_components...` imports inside the package — relative imports.
- **Don't** swallow `get_models` errors into `[]` — that masks a bad API key as "no models". Surface
  the real error to the config/validation layer.
- **Don't** duplicate capability flags between config conditionals and the adapters — put them on the
  provider class and read them.

---

## 3. Streaming (mandatory)

Streaming is not an optimization here — it is the **primary latency lever** for voice (see
`03-the-shim.md` §5). All adapters stream (`stream=True`) and emit normalized text/thinking/tool
deltas as they arrive.

- **OpenAI-compatible SSE**: `data: {json}\n\n` lines, `[DONE]` sentinel. Deltas arrive under
  `choices[0].delta` (`content`, `tool_calls` with incremental `arguments` fragments).
- **Anthropic**: typed event stream (`message_start`, `content_block_delta` with
  `text_delta`/`thinking_delta`/`input_json_delta`, `signature_delta`, `message_delta`, …).
- **Tool-call arguments stream as string fragments** and must be concatenated then parsed — they are
  often invalid until the final fragment. Budget for a repair pass (§5).
- Treat SSE bytes as **untrusted wire data** — the parsers are a good target for fuzzing.
- **Provide a non-streaming fallback** for proxies that don't do SSE cleanly (a gap the prior repo
  had). Decide per backend.

---

## 4. Tool calling (the agent loop primitive)

Inside HA, `ChatLog` owns the loop (see `01` §3). In the **middleman/external** agent you own it:

1. Send messages + tool schemas to the LLM (`stream=True`).
2. Accumulate the streamed assistant text (emit it onward immediately for early TTS) and any
   `tool_calls`.
3. When a tool call completes, execute it (for the shim/middleman: via the **HA MCP client** — see
   `03-the-shim.md` §4), append the result as a tool message, loop.
4. Stop when the model returns a final answer with no new tool calls, or the **iteration cap** is
   hit.

**Iteration caps differ by surface** (a concrete lesson):
- **Voice / interactive conversation**: shallow, ~**3–10**. A human is waiting; dead air kills UX.
- **Autonomous / AI-Task background jobs**: deep, up to ~**1000**. No human waiting.

Split these; don't use one constant.

---

## 5. Small / open-weight model hardening

Open-weight models behind llama-swap/Ollama fail in specific, recurring ways. HA's `ollama`
integration ships fixes worth copying (the prior repo's commit log — "tool crash without LLM API",
"memory replay loss" — shows it hit exactly these):

- **`_fix_invalid_arguments`** — models emit tool arguments as a *stringified* JSON blob, or with
  trailing junk; detect and re-parse.
- **`_parse_tool_args`** — drop empty/`None` argument keys the model hallucinates.
- **`_trim_history`** — keep the system message + last *N* turn pairs to stay under the context
  window. Prefer **message-count trimming** over a `len(text)//4` token estimate (the prior repo's
  `//4` heuristic was imprecise — over-pruned or overflowed).

---

## 6. Structured output (two-tier, because support is heterogeneous)

Local OpenAI-compatible backends' `response_format: json_schema` support is **inconsistent and
unadvertised** — some honor it, some silently ignore it. Even when accepted, small models frequently
emit non-conforming JSON (see the linked `gemma-3-27b-it` bug in `01` §4). Strategy:

1. **Native mode** — pass `response_format`/`json_schema` (OpenAI-compatible) or Anthropic's
   `output_config` **when the model/backend is known to support it**.
2. **Forced-tool fallback** — otherwise, expose a single synthetic tool whose parameters *are* the
   schema, and force the model to call it.
3. **Always validate** the result (`json_loads` + schema check) and fail loudly on non-conformance —
   never trust the model honored the schema.

Schemas in HA come as **`voluptuous` schemas built from selectors**, converted with
`voluptuous_openapi.convert(...)`; OpenAI-style strict mode wants `additionalProperties: false` +
all-required. Local backends differ — verify per target.

---

## 7. Anthropic specifics (extended thinking + tool use)

- Claude's **extended thinking** blocks carry an opaque **`signature`** (and sometimes encrypted
  payload). To continue a thinking + tool-use conversation, you must **replay the thinking block
  *with* its signature** on the next turn, not just the visible text. The prior repo *discarded*
  `signature_delta` — flagged as a likely correctness bug (**inferred**; verify against current
  Anthropic docs before relying on it). If you can't preserve the signature, drop the thinking block
  entirely rather than replay it unsigned.
- In HA terms this maps to `AssistantContent.native` (the opaque provider state deliberately withheld
  from delta listeners). Outside HA, keep your own equivalent "opaque provider continuation" field.
- Anthropic also supports **prompt caching** and **native `json_schema`** output with a forced-tool
  fallback — worth adopting.

---

## 8. Resilience patterns worth keeping (from the prior repo)

- **Retry + sticky model-fallback**: distinguish **retryable** (429, 5xx, timeout — honor
  `Retry-After`) from **terminal** (400/401/404 — never retry, never fail over on auth). Keep the
  fallback model **sticky** across tool iterations within a turn so you don't flip-flop mid-turn.
- **Fail fast, loudly** on terminal errors so the consumer (shim → HA pipeline) can fall back rather
  than hang.
- **Usage/token accounting** captured from the stream (prompt/completion tokens) for an observability
  surface.

---

## 9. Client choices for the middleman (decision, see `05`)

Three ways to talk to the LLMs from the external service:
1. **Port the prior repo's `Protocol`/adapter code** — proven, minimal deps, you own every byte.
2. **Official SDKs** (`openai`, `anthropic`) — less code, but two SDKs and their churn.
3. **LangChain/LangGraph model wrappers** — most helpers (checkpointer memory, deep-agent graph),
   heaviest dependency tree; justified mainly if you want the deep-agent capability. Belongs in the
   external service, never inside HA.

Recommendation and rationale in `05-architecture-decisions-and-tradeoffs.md` §"LLM client".
