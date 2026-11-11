---
type: gotcha
title: The ChatLog timestamp prefix now applies only to presets that forward bare text
description: The ChatLog timestamp prefix now applies only to presets that forward bare turn text; openai_compat/ollama replay ChatLog, n8n/langgraph forward the rendered prompt, so all four opt out.
tags: [home-assistant, chat-log, conversation, adapters]
generated: {by: okf-wiki/unknown, at: 2026-11-10T14:23:08Z}
verified:
  - {by: okf-wiki/opus, at: 2026-09-26T14:46:55Z, commit: 0c763fbee1c0}
  - {by: okf-wiki/deepseek, at: 2026-10-25T07:10:57Z, commit: e2b31f41a3ed}
  - {by: okf-wiki/unknown, at: 2026-11-10T14:23:08Z, commit: 0d0722253f00}
sources:
  - {id: s1, resource: "homeassistant/components/conversation/entity.py and chat_log.py (HA 2026.7.1, the locked dev dependency)", title: async_process opens async_get_chat_log before the handler}
  - {id: s2, resource: https://github.com/allada-homelab/LLM-Middleman/pull/50, title: Prepend timestamp to user messages for temporal context}
  - {id: s3, resource: custom_components/llm_middleman/backends/openai_compat.py, title: stateless ChatLog replay}
  - {id: s4, resource: commit:82cbc5f, title: Prefix the turn time only for presets that forward bare text}
---

# The ChatLog timestamp prefix now applies only to presets that forward bare text

## Symptom

A per-turn rewrite of the user's utterance, done by assigning `user_input.text` inside
`LLMMiddlemanConversationEntity._async_handle_message`, shows up at some backends and not
others. The timestamp prefix added for temporal context[^s2] is the live case: since commit
82cbc5f[^s4] the split is explicit — each adapter declares `wants_time_prefix` (default
`True` in `base.py`), so the prefix reaches only the presets that forward bare turn text
(`converse`, `dify`) and never `openai_compat`, `ollama`, `n8n` or `langgraph`.
`tests/test_conversation.py` pins the keepers to `{converse, dify}`, so a regression now
fails the suite.

## What fails

`ConversationEntity.async_process` opens `async_get_chat_log(hass, session, user_input)`
before it calls `_async_handle_message`, and that context manager appends
`UserContent(content=user_input.text)` on entry[^s1]. The ChatLog therefore already holds
the original text when the entity runs. The stateless-replay adapters rebuild `messages[]`
from `chat_log.content`[^s3] (the ollama adapter does the same), so an assignment to
`user_input.text` afterwards never reaches them. `n8n` and `langgraph` do read
`user_input.text`, but they forward the rendered system prompt — which already carries HA's
date/time — so the prefix would only duplicate temporal context, and they opt out too. Only
`converse` and `dify`, which forward the bare turn text, keep the prefix.

## What works

Decide which surface the change must reach before writing it:

- For every preset, the rewrite has to land in the ChatLog's user content too (or be applied
  in each adapter's message builder), not only on `user_input`.
- Adapters that replay `chat_log` or forward the rendered system prompt declare
  `wants_time_prefix = False`; only `converse` and `dify` keep the default `True` in
  `base.py`. The timestamp itself is built with `dt_util.now()` in HA's configured timezone.
- When adding a new adapter, check which of the two sources it reads; mixing them is how the
  presets drift apart.

## Why

HA owns the ChatLog lifecycle so the history, tool results and debug views stay consistent
across agents. `ConversationInput` is a mutable dataclass, so the assignment succeeds without
error and looks like it worked.

## Verify

- `custom_components/llm_middleman/conversation.py` :: `if self.adapter.wants_time_prefix:`
- `custom_components/llm_middleman/conversation.py` :: `user_input.text = f"[`
- `custom_components/llm_middleman/backends/base.py` :: `wants_time_prefix: ClassVar[bool] = True`
- `custom_components/llm_middleman/backends/openai_compat.py` :: `wants_time_prefix: ClassVar[bool] = False`
- `custom_components/llm_middleman/backends/ollama.py` :: `wants_time_prefix: ClassVar[bool] = False`
- `custom_components/llm_middleman/backends/n8n.py` :: `wants_time_prefix: ClassVar[bool] = False`
- `custom_components/llm_middleman/backends/langgraph.py` :: `wants_time_prefix: ClassVar[bool] = False`
- `tests/test_conversation.py` :: `test_time_prefix_opt_out_for_prompt_forwarding_and_replay_presets`
