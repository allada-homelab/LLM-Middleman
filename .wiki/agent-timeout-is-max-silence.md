---
type: decision
title: The agent Timeout is the maximum backend silence, not a total deadline
description: The per-agent Timeout bounds how long a backend may stay silent (aiohttp sock_read), never a total deadline or a fixed idle cap; both broke slow backends.
tags: [adapters, conversation]
generated: {by: okf-wiki/haiku, at: 2026-10-25T04:42:34Z}
verified:
  - {by: okf-wiki/haiku, at: 2026-10-25T04:42:34Z, commit: 16dde55938d9}
  - {by: okf-wiki/deepseek, at: 2026-10-25T07:14:05Z, commit: e2b31f41a3ed}
sources:
  - {id: s1, resource: https://github.com/allada-homelab/LLM-Middleman/pull/110, title: PR 110 makes sock_read the agent Timeout as the one knob}
  - {id: s2, resource: https://github.com/allada-homelab/LLM-Middleman/pull/84, title: PR 84 (v1.1.4) drops total but hardcodes a 30 s sock_read}
  - {id: s3, resource: custom_components/llm_middleman/backends/base.py, title: build_client_timeout sets total=None with sock_connect and sock_read from the agent Timeout}
  - {id: s4, resource: tests/test_streaming_e2e.py, title: guard tests for silence and live-stream timeouts}
---

# The agent Timeout is the maximum backend silence, not a total deadline

## Decision

The per-agent `Timeout` is both `sock_connect` and `sock_read` on one `aiohttp.ClientTimeout(total=None, ...)`[^s3]. It caps how long the backend may stay silent, before the reply starts (a blocking reply, prompt processing, a model load) or between streamed frames. A live stream runs as long as frames keep arriving.

## Why

v0 used `total=timeout`, which cut long streamed replies at 60 s. PR 84 (v1.1.4) removed `total` but hardcoded `sock_read` to a 30 s idle constant[^s2]. That regression shipped through 1.3.0: any backend silent for more than 30 s failed (n8n, CPU prompt processing, reasoning models, Ollama model loads), and raising Timeout had no effect. PR 110 made `sock_read` the agent Timeout instead[^s1]. Guard tests cover silence shorter and longer than the Timeout and a live stream outlasting it[^s4]. Probes, stop and cancel keep short total caps.

## Rejected alternatives

- Restoring `total=` as a deadline: it cuts off replies that are still streaming.
- A fixed idle constant: it ignores the user's setting, which is the 30 s regression.
- A separate "max silence" option: rejected as an extra concept, since one Timeout knob already covers it[^s1].

## Revisit when

A backend needs a whole-request deadline, or a separate silence limit distinct from the Timeout.

## Verify

- `custom_components/llm_middleman/backends/base.py` :: `build_client_timeout`
