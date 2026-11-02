---
type: gotcha
title: ChatLog snapshots the user text before _async_handle_message runs
description: HA adds the user turn to ChatLog before calling the entity, so editing user_input.text there reaches only presets that read user_input; openai_compat and ollama replay ChatLog.
tags: [home-assistant, chat-log, conversation, adapters]
generated: {by: okf-wiki/deepseek, at: 2026-10-25T07:10:57Z}
verified:
  - {by: okf-wiki/opus, at: 2026-09-26T14:46:55Z, commit: 0c763fbee1c0}
  - {by: okf-wiki/deepseek, at: 2026-10-25T07:10:57Z, commit: e2b31f41a3ed}
sources:
  - {id: s1, resource: "homeassistant/components/conversation/entity.py and chat_log.py (HA 2026.7.1, the locked dev dependency)", title: async_process opens async_get_chat_log before the handler}
  - {id: s2, resource: https://github.com/allada-homelab/LLM-Middleman/pull/50, title: Prepend timestamp to user messages for temporal context}
  - {id: s3, resource: custom_components/llm_middleman/backends/openai_compat.py, title: stateless ChatLog replay}
---

# ChatLog snapshots the user text before _async_handle_message runs

## Symptom

A per-turn rewrite of the user's utterance, done by assigning `user_input.text` inside
`LLMMiddlemanConversationEntity._async_handle_message`, shows up at some backends and not
others. The timestamp prefix added for temporal context[^s2] is the live case: it reaches the
converse and Dify backends but never openai_compat, ollama, n8n or langgraph. Since commit
82cbc5f the split is explicit — each adapter declares `wants_time_prefix` — and
`tests/test_conversation.py` covers both sides, so a regression now fails.

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
- Adapters that replay `chat_log` or forward the rendered system prompt (which already
  carries HA's date/time) declare `wants_time_prefix = False`; only `converse` and `dify`,
  which forward the bare turn text, keep the default `True` in `base.py`.

When adding a new adapter, check which of the two sources it reads; mixing them is how the
presets drift apart.

## Why

HA owns the ChatLog lifecycle so the history, tool results and debug views stay consistent
across agents. `ConversationInput` is a mutable dataclass, so the assignment succeeds without
error and looks like it worked.

## Verify

- `custom_components/llm_middleman/conversation.py` :: `wants_time_prefix`
- `custom_components/llm_middleman/conversation.py` :: `user_input.text = f"[`
- `custom_components/llm_middleman/backends/base.py` :: `wants_time_prefix: ClassVar[bool] = True`
- `custom_components/llm_middleman/backends/openai_compat.py` :: `messages = [_convert_content(item, item_images) for item, item_images in zip(items, images, strict=True)]`
- `custom_components/llm_middleman/backends/ollama.py` :: `items = replayable_content(chat_log.content)`
- `custom_components/llm_middleman/backends/dify.py` :: `"query": user_input.text`
- `custom_components/llm_middleman/backends/n8n.py` :: `input_field: user_input.text`
- `tests/test_conversation.py` :: `test_time_prefix_opt_out_for_prompt_forwarding_and_replay_presets`
