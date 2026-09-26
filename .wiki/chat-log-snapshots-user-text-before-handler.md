---
type: gotcha
title: ChatLog snapshots the user text before _async_handle_message runs
description: HA adds the user turn to ChatLog before calling the entity, so editing user_input.text there reaches only presets that read user_input; openai_compat and ollama replay ChatLog.
tags: [home-assistant, chat-log, conversation, adapters]
generated: {by: okf-wiki/opus, at: 2026-09-26T14:46:55Z}
verified:
  - {by: okf-wiki/opus, at: 2026-09-26T14:46:55Z, commit: 0c763fbee1c0}
sources:
  - {id: s1, resource: "homeassistant/components/conversation/entity.py and chat_log.py (HA 2026.7.1, the locked dev dependency)", title: async_process opens async_get_chat_log before the handler}
  - {id: s2, resource: https://github.com/allada-homelab/LLM-Middleman/pull/50, title: Prepend timestamp to user messages for temporal context}
  - {id: s3, resource: custom_components/llm_middleman/backends/openai_compat.py, title: stateless ChatLog replay}
---

# ChatLog snapshots the user text before _async_handle_message runs

## Symptom

A per-turn rewrite of the user's utterance, done by assigning `user_input.text` inside
`LLMMiddlemanConversationEntity._async_handle_message`, shows up at some backends and not
others. The timestamp prefix added for temporal context[^s2] is the live case: it is sent to
the converse, langgraph, n8n and Dify backends but never to openai_compat or ollama.
No test covers the prefix, so nothing fails.

## What fails

`ConversationEntity.async_process` opens `async_get_chat_log(hass, session, user_input)`
before it calls `_async_handle_message`, and that context manager appends
`UserContent(content=user_input.text)` on entry[^s1]. The ChatLog therefore already holds
the original text when the entity runs. The stateless-replay adapters rebuild `messages[]`
from `chat_log.content`[^s3] (the ollama adapter does the same), so an assignment to
`user_input.text` afterwards never reaches them. The stateful adapters build their request
from `user_input.text` directly and do see it.

## What works

Decide which surface the change must reach before writing it:

- For every preset, the rewrite has to land in the ChatLog's user content too (or be applied
  in each adapter's message builder), not only on `user_input`.
- For stateful presets only, `user_input.text` is enough, but say so in the code.

When adding a new adapter, check which of the two sources it reads; mixing them is how the
presets drift apart.

## Why

HA owns the ChatLog lifecycle so the history, tool results and debug views stay consistent
across agents. `ConversationInput` is a mutable dataclass, so the assignment succeeds without
error and looks like it worked.

## Verify

- `custom_components/llm_middleman/conversation.py` :: `user_input.text = f"[`
- `custom_components/llm_middleman/backends/openai_compat.py` :: `messages = [_convert_content(item) for item in chat_log.content]`
- `custom_components/llm_middleman/backends/ollama.py` :: `for content in chat_log.content`
- `custom_components/llm_middleman/backends/dify.py` :: `"query": user_input.text`
- `custom_components/llm_middleman/backends/n8n.py` :: `input_field: user_input.text`
