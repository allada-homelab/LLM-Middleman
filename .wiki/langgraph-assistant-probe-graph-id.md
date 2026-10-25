---
type: gotcha
title: LangGraph assistant probe rejects graph ids
description: LangGraph GET /assistants/{id} accepts only UUIDs and answers 422 for a graph id, so the config probe must POST /assistants/search with graph_id when the assistant is a graph name.
tags: [adapters, config-flow]
generated: {by: okf-wiki/haiku, at: 2026-10-24T23:52:06Z}
verified:
  - {by: okf-wiki/haiku, at: 2026-10-24T23:52:06Z, commit: ad14312f1a7c}
  - {by: okf-wiki/deepseek, at: 2026-10-25T07:10:57Z, commit: e2b31f41a3ed}
sources:
  - {id: s1, resource: https://github.com/allada-homelab/LLM-Middleman/pull/99, title: "LLMM-022 probe fix, verified live against langgraph-api 0.16.6"}
  - {id: s2, resource: custom_components/llm_middleman/backends/langgraph.py, title: _probe_assistant and the UUID branch in the config probe}
---

# LangGraph assistant probe rejects graph ids

## Symptom

A LangGraph connection whose assistant is set to a graph name such as `agent` (the default
assistant id) used to pass the config flow, then failed with HTTP 422 on the first voice
turn.[^s1] The old `/ok` probe answered 200 even for a wrong id, so nothing warned at setup.

## What fails

`GET /assistants/{id}` takes only an assistant UUID. Given a graph id it returns 422, so a
probe built on that route rejects a valid graph name.[^s1]

## What works

The probe branches on the id. A UUID goes to `GET /assistants/{id}`. Anything else goes to
`POST /assistants/search` with `{"graph_id": id, "limit": 1}`; an empty result list raises
`AssistantNotFoundError`, so a wrong graph name fails at setup, not on the first turn.[^s2]

## Why

LangGraph exposes assistants by UUID on the item route and by graph on the search route.
The adapter has to pick the route that matches the value the user typed, because the config
field accepts both forms.[^s2]

## Verify

- `custom_components/llm_middleman/backends/langgraph.py` :: `_probe_assistant`
