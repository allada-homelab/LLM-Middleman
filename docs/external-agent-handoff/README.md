# External Agent Service — Handoff Bundle

A **self-contained** pack for building the *external agent service* (the "brain") from scratch in its
own repository. Everything you need is in these four files — you do **not** need the `LLM-Middleman`
or `LLM-Home-Controller` repositories on disk (they're named only as prior art).

## What you're building

Home Assistant's voice front-end (mic → wake word → STT → **conversation agent** → TTS) stays as-is.
A thin HA integration called **`LLM-Middleman` (the "shim")** — already built and running — plugs into
the pipeline's conversation-agent slot and, instead of running an LLM itself, **forwards each
recognized text turn to your service** over an HTTP+SSE contract, then streams your reply back into
TTS. Your service is the brain: it runs the LLM agent loop, controls the home by calling back into HA
over MCP, and **streams** the reply text back.

```
HA mic → STT ─► shim (ConversationEntity, already built) ──POST /v1/converse──► YOUR SERVICE
                        ▲                                                          │  agent loop over an LLM
  TTS ◄── streamed text │◄──────────────── SSE: text_delta… done ─────────────────┤  MCP client → HA (control home)
                                                                                   │  session/memory per conversation_id
```

Text-only (HA does STT/TTS; audio never reaches you). Streaming is mandatory for acceptable voice
latency.

## Read in this order

1. **`implementation-brief.md`** — the full spec: architecture, the `/v1/converse` contract (§4),
   what your service does per turn, config, build order, security, open decisions.
2. **`mcp-to-home-assistant.md`** — the concrete how-to for the home-control path: connecting to HA's
   `mcp_server` as an MCP client, auth, listing/calling tools, and mapping MCP tools into your LLM's
   tool schema.
3. **`llm-providers.md`** — the LLM provider layer: OpenAI-compatible + Anthropic streaming, the
   adapter pattern, tool-calling, small-model hardening, structured output.

## The one hard rule

**The `/v1/converse` contract (brief §4) is fixed.** The HA shim already implements the consumer side
of it. Treat it as a frozen interface — don't change the endpoint, request shape, or SSE event names
unilaterally, or you'll break the shim. Everything *behind* the endpoint is yours to design.

## Status of the facts here

- The contract, architecture, and provider patterns are settled.
- The HA `mcp_server` endpoint/auth details in `mcp-to-home-assistant.md` were researched against
  current HA docs + core source (July 2026); items that need confirming against *your* HA instance
  are marked **VERIFY**. Do that pass before relying on them.
