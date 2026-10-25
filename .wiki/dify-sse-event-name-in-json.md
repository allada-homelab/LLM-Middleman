---
type: gotcha
title: Dify SSE events are dispatched on the JSON event field
description: Dify sends bare data frames with the event name inside the JSON, so dispatch on payload event; an Agent app's closing message repeats the answer and must not be spoken again.
tags: [adapters, conversation]
generated: {by: okf-wiki/haiku, at: 2026-10-24T21:55:44Z}
verified:
  - {by: okf-wiki/haiku, at: 2026-10-24T21:55:44Z, commit: 30cf56853287}
  - {by: okf-wiki/deepseek, at: 2026-10-25T07:10:57Z, commit: e2b31f41a3ed}
sources:
  - {resource: https://docs.dify.ai/en/api-reference/openapi_service.json, note: "Dify Service API OpenAPI spec, fetched 2026-10-24"}
  - {resource: https://github.com/allada-homelab/LLM-Middleman/pull/86, note: fix that dispatches on payload event}
  - {resource: https://github.com/allada-homelab/LLM-Middleman/pull/91, note: cross-preset contract test using the Dify Agent-app wire}
---

# Dify SSE events are dispatched on the JSON event field

## Symptom

Dify Agent apps spoke their answer twice, mid-stream `error` events were swallowed, and `message_end` and `message_replace` were never recognized.

## What fails

The adapter dispatched on the SSE `event` field, which the framer defaults to `message`. Answer text worked only by accident. Unit fixtures used fabricated `event:` lines, so the tests passed anyway.

## What works

Every chat-messages frame is a bare `data: {json}` line with no `event:` line, except `ping`. The adapter dispatches on `payload["event"]` and falls back to the SSE field only when that is absent. An Agent app's closing `message` repeats the complete answer after its `agent_message` chunks, so it is skipped once any `agent_message` has streamed.

## Why

Dify puts the event name in the JSON, so the framer's default `message` carries no meaning. The OpenAPI spec defines the wire shape. `tests/backends/test_contract.py` replays the Agent-app wire, so a regression to SSE-field dispatch fails its tests.

## Verify

- `custom_components/llm_middleman/backends/dify.py` :: `_EVENT_AGENT_MESSAGE`
