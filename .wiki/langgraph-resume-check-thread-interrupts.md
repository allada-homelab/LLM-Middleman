---
type: gotcha
title: LangGraph resume needs a pending interrupt, not thread status
description: Before resuming a LangGraph interrupt, check GET /threads/{id} has a non-empty interrupts field; status alone misfires because a cancelled run also reads interrupted.
tags: [adapters, home-assistant]
generated: {by: okf-wiki/haiku, at: 2026-10-24T23:52:01Z}
verified:
  - {by: okf-wiki/haiku, at: 2026-10-24T23:52:01Z, commit: ad14312f1a7c}
sources:
  - {id: s1, resource: https://github.com/allada-homelab/LLM-Middleman/pull/99, title: PR 99 adds the LangGraph tier-1 adapter and the resume check}
  - {id: s2, resource: custom_components/llm_middleman/backends/langgraph.py, title: _take_pending_resume confirms the interrupt on the thread}
---

# LangGraph resume needs a pending interrupt, not thread status

## Symptom

After Home Assistant cancelled a LangGraph run, the next user turn was swallowed and never reached the model[^s1].

## What fails

Sending `{"command": {"resume": ...}}` when the thread status is `interrupted` is not enough. A cancelled run also leaves the status `interrupted`, and the server silently ignores a resume sent to a thread with no pending interrupt[^s1].

## What works

Confirm `GET /threads/{id}` returns a non-empty `interrupts` field before sending the resume. Only resume when that field is set; otherwise send a normal input[^s2].

## Why

The adapter sends `on_disconnect: cancel` and a best-effort `runs/{run_id}/cancel`, so a cancelled run leaves the thread looking interrupted. Interrupts are detected in the stream from the `updates` stream mode's `__interrupt__` key[^s1]. The server sends SSE heartbeats about every 5 s, so a 40 s silent tool step does not trip the 30 s `sock_read` idle timeout[^s1].

## Verify

- `custom_components/llm_middleman/backends/langgraph.py` :: `_take_pending_resume`
