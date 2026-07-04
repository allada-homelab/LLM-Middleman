# Knowledge Base — HA passthrough shim + middleman

The complete knowledge dump from the research and design sessions that led to this project. Purpose:
let anyone (human or agent) build the **passthrough shim** (HA Voice → external agent) and the
**middleman** service from the ground up with full context — no need to re-derive anything.

## Read in this order

| # | Doc | What's in it |
|---|-----|--------------|
| 01 | [`01-home-assistant-reference.md`](01-home-assistant-reference.md) | **The authoritative HA 2026.7 reference** — integration architecture, the Conversation/Assist + LLM tool-calling stack (with exact signatures), AI Task, Voice pipeline, MCP, testing & distribution. Also (§7–9) lessons from the prior in-HA implementation. *Start here for any HA API question.* |
| 02 | [`02-llm-backends-and-providers.md`](02-llm-backends-and-providers.md) | Talking to LLMs: OpenAI-compatible + Anthropic, the provider/adapter pattern, streaming, tool-calling, small-model hardening, two-tier structured output, resilience. |
| 03 | [`03-the-shim.md`](03-the-shim.md) | **The shim** — the thin HA `ConversationEntity` that forwards to the middleman: what it is/isn't, HACS structure, HA plumbing, the shim⇄middleman contract, voice latency. |
| 04 | [`04-the-passthrough-plan.md`](04-the-passthrough-plan.md) | **The original passthrough plan**, end-to-end: the idea, the full flow, component responsibilities, control path, phased build order, risks. |
| 05 | [`05-architecture-decisions-and-tradeoffs.md`](05-architecture-decisions-and-tradeoffs.md) | **The decision log** — every consequential choice, the alternatives weighed, pros/cons, what's settled vs still open, and correctness lessons carried forward. |
| 06 | [`06-glossary-and-references.md`](06-glossary-and-references.md) | Glossary of every term, all primary-source URLs, and the live-verification checklist. |

## Related docs in this repo

- [`../plans/middleman-implementation-brief.md`](../plans/middleman-implementation-brief.md) — the
  concrete build brief for the **middleman service** (this repo's scaffold), with the same contract.

## The one-paragraph summary

Home Assistant's voice front-end (mics, wake word, STT, TTS) stays as-is. A thin **shim**
(`ConversationEntity`, in a HACS integration) forwards each recognized utterance to an external
**middleman** service. The middleman runs an LLM agent loop over OpenAI-compatible and/or Anthropic
backends, controls the home by calling back into HA via its stock **`mcp_server`** (MCP client), and
**streams** the reply text back to the shim → TTS. This keeps heavy agent dependencies (e.g.
LangGraph deep agents) out of HA, and is the clean place to run them. Streaming is mandatory (voice
latency); voice turns stay shallow; long-horizon autonomy goes to a non-voice/AI-Task path.

## Status & caveats

- This is **captured knowledge + plans**, not code. No shim/middleman logic is written yet.
- Several load-bearing facts are marked **(verify live)** — see `06` §3 and `01`'s "Least confident"
  section. Confirm against a running HA 2026.7 before depending on them.
- **Structural note:** the shim (a HA integration) needs a HACS `custom_components/` layout; this
  repo is currently a FastAPI **service** scaffold (the *middleman*). Decide where the shim lives
  (`05` Decision 4) before building it here.
