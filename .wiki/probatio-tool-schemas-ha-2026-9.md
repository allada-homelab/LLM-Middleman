---
type: gotcha
title: HA 2026.9+ tool schemas are probatio, not voluptuous
description: From HA 2026.9, llm tool schemas are probatio Schemas that voluptuous_openapi cannot convert, so tool schemas must go through backends/_compat.py schema_to_openapi.
tags: [home-assistant, adapters, conversation]
generated: {by: okf-wiki/haiku, at: 2026-10-24T21:55:50Z}
verified:
  - {by: okf-wiki/haiku, at: 2026-10-24T21:55:50Z, commit: 30cf56853287}
  - {by: okf-wiki/deepseek, at: 2026-10-25T07:10:57Z, commit: e2b31f41a3ed}
sources:
  - {id: s1, resource: https://github.com/allada-homelab/LLM-Middleman/pull/88, title: Shim probatio tool schemas and ToolResultContent.result for HA 2026.9/2026.10}
  - {id: s2, resource: custom_components/llm_middleman/backends/_compat.py, title: schema_to_openapi and tool_result_data shims}
  - {id: s3, resource: "homeassistant/components/ollama/entity.py (HA 2026.10, lines 39-51)", title: upstream _format_tool probatio branch that the shim mirrors}
---

# HA 2026.9+ tool schemas are probatio, not voluptuous

## Symptom

On HA 2026.9 and later, the `openai_compat` and `ollama` tool loop silently broke: HA tool
schemas could not be rendered for the backend.[^s1]

## What fails

From HA 2026.9, `voluptuous` is an alias of `probatio`, and `llm.Tool.parameters` is a
`probatio.Schema`. `voluptuous_openapi.convert(..., custom_serializer=llm.selector_serializer)`
cannot render it. Depending on import order it returns an UNSUPPORTED sentinel, or it raises
`TypeError: unhashable type: 'Schema'` when voluptuous is imported before homeassistant.[^s1]
The second case misleads scratch probes into blaming the import order alone.

## What works

Route every tool schema through `schema_to_openapi` in `backends/_compat.py`. It calls
`probatio.to_openapi(..., openapi_version="3.1.0")` for `probatio.Schema` values and falls
back to `voluptuous_openapi.convert` on HA older than 2026.9.[^s2][^s3]

## Why

HA's own ollama integration renders probatio schemas with `probatio.to_openapi`, so this
shim mirrors that path rather than inventing a converter.[^s3] The same change also covers
the 2026.10 `ToolResultContent.result` rename through `tool_result_data`.[^s1][^s2]

## Verify

- `custom_components/llm_middleman/backends/_compat.py` :: `schema_to_openapi`
