---
type: gotcha
title: Converse streams ending without done were passed off as full replies
description: A converse stream ending without done now raises (was passed off as a full reply), and an empty done no longer speaks the untranslated fallback — both go through the entity guard like Dify.
tags: [adapters, conversation]
generated: {by: okf-wiki/deepseek, at: 2026-11-10T14:08:50Z}
verified:
  - {by: okf-wiki/deepseek, at: 2026-11-10T14:08:50Z, commit: 0d0722253f00}
sources:
  - {id: s1, resource: commit:2e7c9aa, title: "fix(converse): never pass off a dropped stream as complete; surface tool activity (LLMM-025)"}
  - {id: s2, resource: custom_components/llm_middleman/backends/converse.py, title: "stream_turn: done is the only success terminal; EOF without done raises"}
---

# Converse streams ending without done were passed off as full replies

## Symptom

A converse stream that ended silently — dropped connection, proxy cut, crashed backend — produced
a reply that looked complete, and a `done` event carrying no text spoke the raw English fallback
without marking the turn failed, so pipelines and automations could not tell either from a real
answer.

## What fails

- EOF without `done` simply returned, so the entity guard appended the fallback after the partial
  reply but the turn was not marked failed (the guard's "silent end" path yields `role + fallback`
  only when nothing was streamed).
- `done` with an empty `text` yielded `ERROR_MESSAGE` (the untranslated English fallback) instead
  of the translated fallback; `ctx.fallback` was never set, so the result still counted as success.
- `tool_activity` was dropped entirely; the spec's own example sends it *after* the spoken text,
  where flushing it would end the stream on a tool result (re-POSTing the turn) or split the reply.

## What works

`stream_turn` now treats `done` as the only success terminal: an `error` event or plain EOF
raises `BackendStreamError` even after deltas, so the entity guard appends the fallback and marks
the turn failed [^s1] [^s2]. A `done` with nothing to say yields nothing and warns, so the
guard's translated fallback applies. `tool_activity` is buffered and flushed as external tool
deltas right before the first spoken text; activity after text started, or with no text at all,
is traced as `converse_tool_activity` instead.

## Why

The converse preset was the old v0 contract and had drifted from the terminal-state discipline
Dify gained in LLMM-021; the entity guard (`conversation.py`) already had the
mark-failed-plus-fallback paths for `BackendStreamError`, they just were never reached. The
stub and fixtures now cover error-code, truncation, tool-activity, keepalive and empty-done
cases [^s1].

## Verify

- `custom_components/llm_middleman/backends/converse.py` :: `raise BackendStreamError("Converse stream ended without done")`
