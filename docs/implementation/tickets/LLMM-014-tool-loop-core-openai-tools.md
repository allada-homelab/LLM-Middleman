---
id: LLMM-014
title: Tool loop core + OpenAI-compatible tools (LLM API multi-select, CONTROL flag, iteration bound)
status: done
phase: 3
depends_on: [LLMM-005, LLMM-007, LLMM-008]
---

# LLMM-014 — Tool loop core + OpenAI-compatible tools (LLM API multi-select, CONTROL flag, iteration bound)

## Context

Phase 3 turns the text-only entity + OpenAI-compatible adapter into a tool-capable
agent so HA's own LLM tools (device control, and — for free — any tools registered by
HA's MCP-client integration) run inside the conversation turn. This implements
`plan.md` §Conversation entity items 1–2, §Streaming parsers (openai-compat tool_calls),
and the "In v1" bullets under §Adjacent HA AI capabilities (`CONF_LLM_HASS_API` as a
list, `ConversationEntityFeature.CONTROL`).

Line-by-line template: core `openai_conversation/entity.py` (in this repo's venv at
`.venv/lib/python3.14/site-packages/homeassistant/components/openai_conversation/entity.py`).
Read it before starting — the tool loop, `_format_tool`, `_convert_content_to_param`,
`_transform_stream` tool-call branches, and the `chat_log.llm_api` gate are all there.

This ticket adds the tool machinery on top of the LLMM-005 entity and LLMM-008
adapter that already stream text. Ollama's native tool path is a separate ticket
(LLMM-015); it depends on this one for the shared loop plumbing.

## Scope

**In:**
- Subentry flow (LLMM-007): add `CONF_LLM_HASS_API` as a **multi-select list** over
  `llm.async_get_apis(hass)`, offered **only when `adapter.supports_ha_tools`** is true.
- Entity (`conversation.py`): pass the selected LLM API(s) into
  `chat_log.async_provide_llm_data(...)`; set `ConversationEntityFeature.CONTROL` iff an
  LLM API is configured for the subentry; run the bounded tool loop
  (`for _ in range(MAX_TOOL_ITERATIONS)`, break when
  `not chat_log.unresponded_tool_results`).
- OpenAI-compatible adapter (`backends/openai_compat.py`): format HA tool schemas into
  the `tools` request field when `chat_log.llm_api` is set; accumulate streamed
  `tool_calls` fragments by index; emit `{"tool_calls": [llm.ToolInput(...)]}` deltas;
  replay `AssistantContent.tool_calls` + `ToolResultContent` back into the provider
  `messages` array on subsequent iterations.

**Out:**
- Ollama native `tool_calls` parsing + malformed-arg repair → **LLMM-015**.
- Text-only adapters (converse/langgraph/n8n): they never set `chat_log.llm_api`
  (`supports_ha_tools = False`), so they do at most one loop iteration — no change to
  their adapters here.
- Registering our own `llm.API` (out per plan §Adjacent — Skip).
- AI Task `structure`/structured-output tool formatting (fast-follow, not v1).

## Implementation notes

**Constant.** Add `MAX_TOOL_ITERATIONS = 10` (const.py or entity module). This matches
core openai `entity.py:114` and ollama `entity.py:35`; the value is also the HA-side
backstop against a runaway backend (research-2: extended_openai's "Maximum Function
Calls Per Conversation" footgun).

**Adapter capability flag.** `BackendAdapter.supports_ha_tools: ClassVar[bool]` already
exists (LLMM-003 / plan §Adapter interface). `openai_compat.py` sets it `True`;
converse/langgraph/n8n set it `False`.

**Subentry flow (`config_flow.py`, LLMM-007).** Gate the field on the parent entry's
adapter class:
```python
if BACKEND_TO_CLS[entry.data[CONF_BACKEND_TYPE]].supports_ha_tools:
    schema[vol.Optional(CONF_LLM_HASS_API)] = SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=api.id, label=api.name)
                     for api in llm.async_get_apis(self.hass)],
            multiple=True,   # stored as list[str]
        )
    )
```
The list form is intentional (plan §Conversation entity item 1):
`async_provide_llm_data` accepts `str | list[str]`, and a list automatically pulls in
tools from every registered `llm.API` — including one per HA MCP-client entry — so users
get MCP-server tools in the loop with zero extra code. (Core openai uses
`LLM_API_ASSIST` single-select; we deliberately widen to multi-select per the plan.)

**Entity (`conversation.py`, LLMM-005).** In `_async_handle_message`:
1. Resolve `llm_api = subentry.data.get(CONF_LLM_HASS_API) or None` (None when unset or
   backend can't do tools — a falsy empty list must become `None`).
2. `await chat_log.async_provide_llm_data(user_input.as_llm_context(DOMAIN), llm_api,
   system_prompt, user_input.extra_system_prompt)`; wrap in
   `except conversation.ConverseError as err: return err.as_conversation_result()`.
3. Loop (core openai `entity.py:664-716` shape). Build the per-turn `TurnContext` once
   before the loop (stable `memory_key`), reused across iterations:
   ```python
   ctx = TurnContext(
       options=self.subentry.data,
       memory_key=self._derive_memory_key(user_input, chat_log),
   )
   for _ in range(MAX_TOOL_ITERATIONS):
       async for _c in chat_log.async_add_delta_content_stream(
           self.entity_id, _guarded(adapter.stream_turn(chat_log, user_input, ctx))
       ):
           pass
       if not chat_log.unresponded_tool_results:
           break
   ```
   `_guarded` is the LLMM-005 never-hangs wrapper — keep it wrapping every iteration. (A
   fresh `TurnContext` per iteration with the same `memory_key` is equally acceptable; the
   openai-compat adapter never sets `ctx.continue_conversation`.)
4. `return conversation.async_get_result_from_chat_log(user_input, chat_log)`.

**CONTROL feature.** Expose it iff tools are configured, so the pipeline's local-intent
fallback doesn't steal device commands (exact core-openai pattern):
```python
@property
def supported_features(self) -> ConversationEntityFeature:
    if self._subentry_has_llm_api:
        return ConversationEntityFeature.CONTROL
    return ConversationEntityFeature(0)
```
(Import `ConversationEntityFeature` from `homeassistant.components.conversation`.)

**OpenAI-compatible adapter tool support (`backends/openai_compat.py`).**
- **Tool schemas.** When `chat_log.llm_api` is set, build the request `tools` field.
  Chat-completions wire shape (NOT the Responses shape core uses):
  `{"type": "function", "function": {"name": tool.name, "description": tool.description,
  "parameters": convert(tool.parameters, custom_serializer=chat_log.llm_api.custom_serializer)}}`.
  `convert` = `voluptuous_openapi.convert`. Copy the unsupported-key strip from core
  openai `_format_tool` (`entity.py:158-173`): drop `oneOf/anyOf/allOf/enum/not` if the
  server rejects them (keep behind a comment; many OpenAI-compatible servers tolerate
  them — leave in only if a probe shows breakage).
- **Streamed tool_call accumulation.** In chat/completions SSE, `choices[0].delta.tool_calls`
  is a **list of fragments each carrying an `index`**; `id`/`function.name` appear on the
  first fragment for that index, and `function.arguments` streams as a **string
  concatenated across fragments**. Accumulate into a dict keyed by `index`:
  ```python
  tool_calls: dict[int, dict] = {}
  # per fragment:
  slot = tool_calls.setdefault(frag["index"], {"id": "", "name": "", "args": ""})
  slot["id"] += frag.get("id", "")
  fn = frag.get("function", {})
  slot["name"] += fn.get("name", "")
  slot["args"] += fn.get("arguments", "")
  ```
  On `[DONE]` (or `finish_reason == "tool_calls"`), emit one delta:
  `{"tool_calls": [llm.ToolInput(id=s["id"], tool_name=s["name"],
  tool_args=json.loads(s["args"] or "{}")) for s in tool_calls.values()]}`.
  Remember the **role-first** rule (research-2 mistake #3): the first delta of the
  assistant block must carry `{"role": "assistant"}` before any `content`/`tool_calls`.
- **History replay of tool turns.** The adapter is stateless (plan §Adapter interface —
  rebuild from `chat_log.content`). Map `AssistantContent.tool_calls` →
  `message.tool_calls` (`{"id", "type": "function", "function": {"name",
  "arguments": json.dumps(tool_args)}}`) and `ToolResultContent` →
  `{"role": "tool", "tool_call_id": ..., "content": json.dumps(tool_result, default=str)}`.
  Use `default=str` (plan §Serialization; research-2 mistake #1: a `time` object from a
  date/time query is not JSON-serializable otherwise).
- Reference mapping table: research-4 §MESSAGE-FORMAT MAPPING (OpenAI chat/completions
  rows) and core openai `_convert_content_to_param` (`entity.py:176-267`).

## Acceptance criteria

- [x] `CONF_LLM_HASS_API` appears in the conversation subentry form **only** for backends
      whose adapter `supports_ha_tools`; it is a multi-select storing `list[str]`.
      (`config_flow.py` `_build_schema` gate; `test_llm_hass_api_gated`.)
- [x] Entity passes the configured LLM API list to `async_provide_llm_data`; when unset,
      passes `None` and the turn behaves exactly as text-only (regression: LLMM-008
      tests still green). (`conversation.py` `options.get(CONF_LLM_HASS_API) or None`;
      `test_provides_llm_data_without_ha_tools` + full suite green.)
- [x] `ConversationEntityFeature.CONTROL` is reported iff the subentry has an LLM API
      configured, and not otherwise. (`__init__` sets `_attr_supported_features`;
      `test_control_feature_present_with_llm_api`.)
- [x] The tool loop runs ≤ `MAX_TOOL_ITERATIONS` and breaks when
      `chat_log.unresponded_tool_results` is empty; a text-only subentry does exactly one
      iteration. (`test_tool_loop_round_trip`, `test_text_only_turn_runs_one_iteration`,
      `test_tool_loop_iteration_cap`.)
- [x] OpenAI-compatible adapter sends a `tools` array when an LLM API is set, accumulates
      streamed `tool_calls` fragments by `index`, emits `llm.ToolInput` deltas, and
      replays prior tool calls/results (with `json.dumps(..., default=str)`) on the next
      iteration. (`test_tools_field_sent_when_llm_api_set`,
      `test_tool_calls_reassembled_across_chunks`,
      `test_history_replays_tool_calls_and_results_default_str`.)
- [x] Gates green: `just check` + `just typecheck` (199 passed; basedpyright 0 errors;
      lint/format/lock clean).

## Verification

Write and run (`just test`) unit tests that drive **raw SSE bytes** through the real
adapter parser (chunk splits mid-`tool_calls`, split `arguments` string across ≥2 `data:`
lines, CRLF line endings), asserting on `MockChatLog` content (LLMM-004 harness):

1. **Fragmented tool_call reassembly** — feed a two-tool-call stream where each call's
   `arguments` string arrives across three chunks and `index` interleaves the two calls;
   assert exactly two `llm.ToolInput` with correct `id`/`tool_name`/`tool_args` (parsed
   dict), role-first delta present.
2. **Tool loop round-trip** — MockChatLog whose `llm_api` has a fake tool; adapter stream
   1 yields a tool_call, HA executes it (MockChatLog appends a `ToolResultContent`),
   `unresponded_tool_results` non-empty → loop re-enters; stream 2 (fake) yields plain
   text and no tool call → loop breaks. Assert 2 iterations, final assistant text present,
   and that the replayed provider `messages` for iteration 2 include the tool-result role
   with a `default=str`-serialized body (feed a `datetime`/`time` value to prove it
   doesn't raise).
3. **Iteration cap** — an adapter fake that always returns a tool_call must stop after
   exactly `MAX_TOOL_ITERATIONS` and still return a `ConversationResult` (no hang).
4. **CONTROL gating** — instantiate the entity for a subentry with and without an LLM
   API; assert `supported_features` differs accordingly.
5. **Config-flow gating** — subentry flow for an openai-compat parent offers the field;
   for a converse parent (supports_ha_tools=False) it does not. Assert on the schema.

### Executed (evidence)

Tests written and passing (`uv run pytest`, full suite **199 passed**, 2 pre-existing
aiohttp BasicAuth deprecation warnings; baseline was 189):

- `tests/backends/test_openai_compat.py`: `test_tool_calls_reassembled_across_chunks`
  (two interleaved calls, args split across frames, byte-at-a-time + CRLF),
  `test_tool_call_with_text_then_call`, `test_empty_arguments_default_to_empty_object`,
  `test_tools_field_sent_when_llm_api_set`, `test_tools_field_absent_without_llm_api`,
  `test_history_replays_tool_calls_and_results_default_str` (datetime tool result →
  `default=str`, no raise).
- `tests/test_conversation.py`: `test_control_feature_present_with_llm_api`,
  `test_tool_loop_round_trip` (real adapter, 2 iterations, turn-2 body carries the
  serialized tool result), `test_text_only_turn_runs_one_iteration`,
  `test_tool_loop_iteration_cap` (stops at `MAX_TOOL_ITERATIONS`, returns a result).
- `tests/test_config_flow.py`: `test_llm_hass_api_gated` (multi-select present only for
  a tool-capable parent).

Gates: `just check` (lock-check + lint + fmt-check + test) green; `just typecheck`
0 errors. `just pre-commit` on the changed files passes; the repo-wide pre-commit
failures (LICENSE end-of-file, codespell in `LLMM-017`/`LLMM-019`/`plan.md`) are
pre-existing and outside this ticket's files.

### In-ticket amendment (small scope)

The bounded loop uses a `for … else`: if the cap is exhausted while the last content is
still an unresponded tool result (a runaway tool-calling backend), the entity logs a
warning and appends an `ERROR_MESSAGE` assistant turn so `async_get_result_from_chat_log`
returns a `ConversationResult` instead of raising `HomeAssistantError`. This extends the
LLMM-005 never-hang guarantee to the tool loop and satisfies the "still return a
`ConversationResult`" clause of Verification item 3.

## Risks / open questions

- **plan implementation-time checkpoint (must do):** confirm against the pinned HA source
  in this repo's venv the exact delta-stream tool-execution flow —
  `chat_log.async_add_delta_content_stream(...)` consuming the generator, HA executing
  tools and appending `ToolResultContent`, and `chat_log.unresponded_tool_results`
  gating the loop — before finalizing. Read
  `.venv/.../homeassistant/components/conversation/chat_log.py` and confirm the loop
  contract matches core openai `entity.py:664-716`. Also confirm whether
  `AbstractConversationAgent` is still needed alongside `ConversationEntity` (plan
  checkpoint; LLMM-005 owns the base-class decision — flag if it affects the loop).
- OpenAI-compatible servers vary: some don't stream `id` on the first `tool_calls`
  fragment, or omit `finish_reason: tool_calls`. The `[DONE]`-sentinel flush plus
  index-keyed accumulation should tolerate this; verify against the owner's llama.cpp
  proxy in LLMM-018.
- The unsupported-JSON-Schema-key strip is a judgment call — leave enabled only if the
  target server actually 400s on `enum`/`anyOf`; otherwise it silently degrades tool
  schemas. Decide with a live probe, not from memory.
