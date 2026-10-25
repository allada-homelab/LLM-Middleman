---
type: gotcha
title: n8n keepalive frames and Chat Trigger probe hazard
description: n8n 2.4x sends a keepalive line every 30 s, possibly first, so the NDJSON sniffer must accept it; a setup probe must never GET a Chat Trigger URL, which starts a run.
tags: [adapters, conversation]
generated: {by: okf-wiki/haiku, at: 2026-10-25T04:42:30Z}
verified:
  - {by: okf-wiki/haiku, at: 2026-10-25T04:42:30Z, commit: 16dde55938d9}
sources:
  - {id: s1, resource: "PR 110 hotfix, verified live against n8nio/n8n:2.44.6 (released 2026-10-22)", title: keepalive-first stream failed as not a JSON object before the fix}
  - {id: s2, resource: "n8n packages/cli streaming-webhook-response-heartbeat.ts (n8n PR #39948, 2026-10-02)", title: keepalive heartbeat source}
  - {id: s3, resource: n8n packages/workflow/src/interfaces.ts, title: StructuredChunk type reference}
  - {id: s4, resource: custom_components/llm_middleman/backends/n8n.py, title: "n8n adapter sniffer, body builder and no-probe docstring"}
---

# n8n keepalive frames and Chat Trigger probe hazard

## Symptom

A streamed n8n reply fails as "not a JSON object" on its first line. The stream is healthy
apart from a heartbeat line sent before any content.

## What fails

n8n 2.4x sends `{"type":"keepalive"}` every 30 s, and the first line of a stream can be one
of them[^s1][^s2]. A sniffer that accepts only `begin`/`item`/`end`/`error` misclassifies the
stream as blocking JSON, or fails the turn. Separately, a setup-time `GET` against a Chat
Trigger URL starts a real workflow run, because the trigger treats every hit as a chat turn.
A plain Webhook only answers `GET` with "not registered for GET".

## What works

`keepalive` is listed in `_STRUCTURED_CHUNK_TYPES`, so a keepalive first line is sniffed as
NDJSON and yields nothing[^s4]. The setup probe (LLMM-025) never sends `GET` or `POST`: it
sends `OPTIONS` with an `Origin` header, which n8n answers from the registered methods
(`Access-Control-Allow-Methods`) before executing anything. On 2.44.6 executions stayed
unchanged across probes of a Webhook and a Chat Trigger. An inactive workflow lists only
`OPTIONS`. Never add `Access-Control-Request-Method`: n8n answers 500 for an unknown path with
it[^s4].

## Why

The chunk envelope (`type`, `content?`, `metadata` with `nodeId`, `nodeName`, `runIndex`,
`itemIndex`, `timestamp`) is not in n8n's public docs; the source is the reference[^s3]. In
n8n's `webhook-request-handler`, `OPTIONS` returns 204 right after the CORS lookup, before
`executeWebhook`, so it is the one method that cannot start a run[^s4].

## Verify

- `custom_components/llm_middleman/backends/n8n.py` :: `_STRUCTURED_CHUNK_TYPES`
- `custom_components/llm_middleman/backends/n8n.py` :: `async_validate_connection`
