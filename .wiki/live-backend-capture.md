---
type: runbook
title: Capture real backend streams inside the devcontainer
description: Real backend byte streams can be captured in the devcontainer with small images, reached by bridge IP because published ports are unreachable.
tags: [adapters, maintenance]
generated: {by: okf-wiki/haiku, at: 2026-10-25T04:42:28Z}
verified:
  - {by: okf-wiki/haiku, at: 2026-10-25T04:42:28Z, commit: 16dde55938d9}
  - {by: okf-wiki/deepseek, at: 2026-10-25T07:14:05Z, commit: e2b31f41a3ed}
sources:
  - {id: pr110, resource: https://github.com/allada-homelab/LLM-Middleman/pull/110}
  - {id: audit, resource: 2026-10-25 Tier-D audits of openai_compat/ollama and n8n/converse}
  - {id: rig, resource: scripts/e2e/README.md}
---

# Capture real backend streams inside the devcontainer

## When

You need real wire bytes from a backend, not hand-written mocks, to pin an adapter's fixtures.
Examples are re-checking the openai_compat/ollama or n8n/converse streams after a parser change,
such as the timeout hotfix in PR 110 [^pr110].

## Steps

1. Pull only small images. For ollama and openai_compat, use `alpine/ollama:0.40.6` (about 32 MB,
   CPU) with `qwen3:0.6b` (about 523 MB). For a second OpenAI-compatible source, use
   `ghcr.io/ggml-org/llama.cpp:server` (about 295 MB) on the same GGUF. For n8n, use
   `n8nio/n8n:2.44.6` (about 280 MB). Do not use the official `ollama/ollama` image, which is about
   3.4 GB [^audit].
2. Name every container `llmm-*` and start it with `docker run -d --name llmm-<x> ...`.
3. Do not use `localhost:<port>` or published ports; they are unreachable from the devcontainer.
   Get the bridge IP with
   `docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' llmm-<x>` and
   call `http://<ip>:<port>`.
4. For n8n streams with no LLM, build a Webhook or Chat Trigger in streaming mode, followed by
   Respond to Webhook with `enableStreaming`. Activate it with
   `POST /rest/workflows/{id}/activate`, because a PATCH does not activate in n8n 2.x. Webhooks
   return 404 for about 5 s after activation, so wait before calling them [^audit].
5. To capture a keepalive frame, put a Wait node of 45 s in the flow. A Wait of 65 s or more
   offloads the execution and returns an empty 200 immediately [^audit].
6. Remove every container when done with `docker rm -f llmm-<x>`.

## Check it worked

Each capture is non-empty and shows the backend's framing (SSE `data:` lines or NDJSON). An n8n
capture made with the 45 s Wait includes keepalive frames.

## Limits

Mid-stream error frames cannot be provoked on Ollama or llama.cpp. Write those fixtures from the
documented vLLM, OpenRouter, and Ollama shapes, and label them spec-derived, not captured.
The E2E rig's own recipes are in `scripts/e2e/README.md` [^rig].

## Verify

- `scripts/e2e/README.md` :: `Per-preset backend recipes`
