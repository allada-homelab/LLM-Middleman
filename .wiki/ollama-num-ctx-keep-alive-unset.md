---
type: decision
title: Ollama backend must not force num_ctx/keep_alive when the user leaves them unset
description: Ollama num_ctx/keep_alive are sent only when configured — HA's 8192/-1 defaults overrode OLLAMA_KEEP_ALIVE and VRAM-sized contexts; the 30s-idle reason died with the Timeout fix.
tags: [adapters, config-flow]
generated: {by: okf-wiki/deepseek, at: 2026-11-10T14:08:50Z}
verified:
  - {by: okf-wiki/deepseek, at: 2026-11-10T14:08:50Z, commit: 0d0722253f00}
sources:
  - {id: s1, resource: commit:b31c019, title: "fix(ollama): stop forcing num_ctx/keep_alive when unset (LLMM-024 item 7)"}
  - {id: s2, resource: README.md, title: "Options table: Context window / Keep alive default to the model / Ollama default"}
---

# Ollama backend must not force num_ctx/keep_alive when the user leaves them unset

## Decision

`_build_request` in `backends/ollama.py` adds `num_ctx` and `keep_alive` to the request body only
when the user configured them (`if CONF_NUM_CTX in options:` / `if CONF_KEEP_ALIVE in options:`);
unset means not sent, so the Ollama server's own configuration applies [^s1]. The config flow
still offers both fields, but empty no longer sends core HA's defaults.

## Why

Before, the adapter filled unset options with core HA's `DEFAULT_NUM_CTX = 8192` /
`DEFAULT_KEEP_ALIVE = -1` so both integrations loaded a model the same way. That overrode the
server's config: it shrank `num_ctx` on big-GPU hosts where newer Ollama sizes the context by
VRAM, and pinned models in VRAM against `OLLAMA_KEEP_ALIVE`. The reason the defaults existed —
a model load tripping the 30 s idle timeout — is gone since #110 made the agent Timeout the
longest allowed silence (see `agent-timeout-is-max-silence`). The README now lists both as
model/Ollama default, recommending at least 8192 with *Control Home Assistant* and a keep
alive when model loads make replies slow [^s2].

## Rejected alternatives

- Keeping core HA's defaults: repeats the override of the user's server configuration.
- Guessing the server's intended values in the adapter: VRAM sizing and `OLLAMA_KEEP_ALIVE`
  live in the server's config; duplicating that logic would drift from it.

## Revisit when

The Timeout model changes again (e.g. a whole-request deadline returns and model loads need
protection), or core HA redefines what an unset option means.

## Verify

- `custom_components/llm_middleman/backends/ollama.py` :: `if CONF_NUM_CTX in options:`
