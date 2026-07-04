# Knowledge Base — the `llm_middleman` HA shim

The complete knowledge dump from the research and design sessions behind this project. Purpose: let
anyone (human or agent) understand and extend the **shim** — the HA-side text-only conversation agent
(`custom_components/llm_middleman/`) that forwards Assist turns to an external agent — plus the spec
for that **external agent** it talks to, with full context and no need to re-derive anything.

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
  build spec for the **external agent** the shim forwards to (a *separate* service, not this repo).

## The one-paragraph summary

Home Assistant's voice front-end (mics, wake word, STT, TTS) stays as-is. **`LLM-Middleman` is the
shim** — a thin, text-only `ConversationEntity` (`custom_components/llm_middleman/`) that forwards
each recognized utterance to a **separate external agent** and streams the reply text back → TTS.
That external agent (spec'd in `../plans/middleman-implementation-brief.md`) runs the LLM agent loop
over OpenAI-compatible/Anthropic backends and controls the home via HA's stock **`mcp_server`** — it
lives in its own repo, keeping heavy agent deps (e.g. LangGraph) out of HA. Text-only (audio
passthrough isn't supported in Assist 2026.7); streaming is mandatory and, since HA's streaming TTS
(Voice Chapter 11, Oct 2025), gives ~0.5 s time-to-first-audio. The built-in Assist chat renders the
conversation for free because the shim is a real `ConversationEntity` writing to `ChatLog`.

## Status & caveats

- **The shim is built.** `custom_components/llm_middleman/` is a working HACS conversation-agent
  integration (gate green). These docs are the design/knowledge behind it; the **external agent** it
  forwards to is a separate, not-yet-built component (see `../plans/middleman-implementation-brief.md`).
- Several load-bearing facts are marked **(verify live)** — see `06` §3 and `01`'s "Least confident"
  section. Confirm against a running HA 2026.7 before depending on them (esp. a real voice smoke test
  and hassfest).
