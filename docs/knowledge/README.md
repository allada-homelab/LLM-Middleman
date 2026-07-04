# Knowledge Base — the `llm_middleman` HA shim

The complete knowledge dump from the research and design sessions behind this project. Purpose: let
anyone (human or agent) understand and extend the **shim** — the HA-side text-only conversation agent
(`custom_components/llm_middleman/`) that forwards Assist turns to an external agent — plus the spec
for that **external agent** it talks to, with full context and no need to re-derive anything.

## Read in this order

| # | Doc | What's in it |
|---|-----|--------------|
| 01 | [`01-home-assistant-reference.md`](01-home-assistant-reference.md) | HA 2026.7 API reference — Conversation/Assist + LLM tool-calling stack, AI Task, Voice pipeline, MCP, testing & distribution. **⚠ Quarantined:** retained verbatim from the sibling `LLM-Home-Controller` repo. Its component-by-component descriptions (a provider Protocol + adapters, `ai_task.py`, usage `sensor.py`, memory tools, `entity.py` line refs) describe **that repo, not this one** — treat only the generic HA-API signatures as authoritative and verify each against this repo's code. See the banner at the top of the file. |
| 02 | [`02-llm-backends-and-providers.md`](02-llm-backends-and-providers.md) | Talking to LLMs: OpenAI-compatible + Anthropic, the provider/adapter pattern, streaming, tool-calling, small-model hardening, two-tier structured output, resilience. |
| 03 | [`03-the-shim.md`](03-the-shim.md) | **The integration** — the backend-agnostic HA `ConversationEntity` that forwards to an external backend: what it is/isn't, HACS structure, HA plumbing, the **five backend presets** (`/v1/converse` is one of them), config model, follow-up listening, memory scope, voice latency. |
| 04 | [`04-the-passthrough-plan.md`](04-the-passthrough-plan.md) | **The original passthrough plan**, end-to-end: the idea, the full flow, component responsibilities, control path, phased build order, risks. |
| 05 | [`05-architecture-decisions-and-tradeoffs.md`](05-architecture-decisions-and-tradeoffs.md) | **The decision log** — every consequential choice, the alternatives weighed, pros/cons, what's settled vs still open, and correctness lessons carried forward. |
| 06 | [`06-glossary-and-references.md`](06-glossary-and-references.md) | Glossary of every term, all primary-source URLs, and the live-verification checklist. |

## Related docs in this repo

- [`../external-agent-handoff/implementation-brief.md`](../external-agent-handoff/implementation-brief.md) — the
  build spec for an **external agent** that implements the custom `/v1/converse` preset (a *separate*
  service, not this repo, and now one of five backend presets — not the boundary contract).

## The one-paragraph summary

Home Assistant's voice front-end (mics, wake word, STT, TTS) stays as-is. **`LLM-Middleman`** is a
thin, text-only `ConversationEntity` (`custom_components/llm_middleman/`) that forwards each recognized
utterance to an **external LLM backend** and streams the reply text back → TTS. In v1 the backend is
chosen from **five presets** (OpenAI-compatible, Ollama, LangGraph, custom `/v1/converse`, n8n) behind
a common adapter layer — so it can point at a self-hosted OpenAI-compatible server, an Ollama server, a
LangGraph deployment, an n8n workflow, or a bespoke external-agent service. The bespoke external-agent
path (spec'd in `../external-agent-handoff/`, LLM loop + `mcp_server` control) is now just the
`/v1/converse` preset, not a required component. Text-only (audio passthrough isn't supported in Assist
2026.7); streaming is mandatory and, since HA's streaming TTS (Voice Chapter 11, Oct 2025), gives ~0.5 s
time-to-first-audio. The built-in Assist chat renders the conversation for free because it is a real
`ConversationEntity` writing to `ChatLog`.

## Status & caveats

- **The integration is built.** `custom_components/llm_middleman/` is a working HACS
  conversation-agent integration (gate green) with all five backend presets. These docs are the
  design/knowledge behind it. An **external agent** implementing the `/v1/converse` preset is a
  separate, not-yet-built (and now optional) component — see `../external-agent-handoff/`.
- Several load-bearing facts are marked **(verify live)** — see `06` §3 and `01`'s "Least confident"
  section. Confirm against a running HA 2026.7 before depending on them (esp. a real voice smoke test
  and hassfest).
