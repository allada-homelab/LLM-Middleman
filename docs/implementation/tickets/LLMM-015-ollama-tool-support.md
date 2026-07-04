---
id: LLMM-015
title: Ollama tool support (native tool_calls + malformed-args repair)
status: todo
phase: 3
depends_on: [LLMM-010, LLMM-014]
---

# LLMM-015 — Ollama tool support (native tool_calls + malformed-args repair)

## Context

Ollama's `/api/chat` carries native `tool_calls` in its NDJSON stream, so the Ollama
adapter (LLMM-010) can join the HA tool loop built in LLMM-014. This implements the
Ollama half of `plan.md` §Streaming parsers (`message.tool_calls`), §Serialization &
safety (malformed-args repair, `json.dumps(default=str)`), and the `CONF_LLM_HASS_API`
capability for Ollama (`supports_ha_tools = True`).

Template: core `ollama/entity.py` in this repo's venv at
`.venv/lib/python3.14/site-packages/homeassistant/components/ollama/entity.py` —
specifically `_format_tool` (`:40-50`), `_fix_invalid_arguments` + `_parse_tool_args`
(`:53-82`), the `_transform_stream` tool-call branch (`:163-170`), and `_convert_content`
tool mapping (`:98-133`). Read it before starting.

This ticket only wires tools into the already-working Ollama NDJSON adapter; the shared
loop, subentry field, and CONTROL flag come from LLMM-014.

## Scope

**In:**
- `backends/ollama.py`: set `supports_ha_tools = True`; format HA tool schemas into the
  `tools` request field when `chat_log.llm_api` is set; parse native `message.tool_calls`
  from the NDJSON stream into `{"tool_calls": [llm.ToolInput(...)]}` deltas with the
  malformed-args repair; replay `AssistantContent.tool_calls` + `ToolResultContent` back
  into the Ollama `messages` array on subsequent iterations using
  `json.dumps(..., default=str)`.
- Enable the `CONF_LLM_HASS_API` multi-select for the Ollama backend (falls out of
  `supports_ha_tools = True` + LLMM-014's capability gate — verify it appears).

**Out:**
- The shared tool loop, `MAX_TOOL_ITERATIONS`, CONTROL flag, and subentry-flow gating —
  all owned by **LLMM-014**; this ticket only flips Ollama's capability flag and adds the
  adapter-side tool plumbing.
- Ollama `format`/structured-output (AI Task fast-follow, not v1).
- Any change to the NDJSON reader itself (LLMM-010 owns it).

## Implementation notes

**Capability flag.** `class OllamaAdapter(BackendAdapter): supports_ha_tools = True`.

**Tool schema formatting.** Ollama tool wire shape differs from OpenAI's — copy core
`_format_tool` (`ollama/entity.py:40-50`) verbatim in spirit:
```python
{"type": "function", "function": {
    "name": tool.name,
    "description": tool.description,          # only if truthy
    "parameters": convert(tool.parameters, custom_serializer=chat_log.llm_api.custom_serializer),
}}
```
`convert` = `voluptuous_openapi.convert`. Pass the list as the client `tools=` kwarg /
request `tools` field only when `chat_log.llm_api` is set (else `None`).

**Native tool_calls in the stream.** Ollama emits tool calls as a whole object inside a
message chunk (`message.tool_calls`), not fragmented like OpenAI. In the adapter's
transform, on a chunk carrying `tool_calls`:
```python
chunk["tool_calls"] = [
    llm.ToolInput(
        tool_name=tc["function"]["name"],
        tool_args=_parse_tool_args(tc["function"]["arguments"]),
    )
    for tc in tool_calls
]
```
Ollama `tool_args` arrive already as a dict (not a JSON string), so no `json.loads`.
Keep the **role-first** rule from LLMM-010's transform (first delta of the block carries
`{"role": "assistant"}`).

**Malformed-args repair (required, plan §Serialization).** Copy
`_fix_invalid_arguments` + `_parse_tool_args` from core `ollama/entity.py:53-82`
verbatim: drop keys whose value is `None` or `""` (they fail HA intent parsing) and
`json.loads` any value that is a stringified JSON array/object. Small local models (e.g.
llama3.1 8B) routinely emit these; without the repair, tool execution fails.

**History replay of tool turns.** Ollama is stateless-replay (LLMM-010 already rebuilds
`messages` from `chat_log.content` and trims via `CONF_MAX_HISTORY`). Extend
`_convert_content` (core `ollama/entity.py:98-133`) to map:
- `AssistantContent` with `tool_calls` → `ollama.Message(role="assistant", content=...,
  tool_calls=[Message.ToolCall.Function(name, arguments)])`.
- `ToolResultContent` → `ollama.Message(role="tool", content=json_dumps(tool_result))`.
Use HA's `homeassistant.helpers.json.json_dumps` (core ollama uses it — it handles HA
objects) OR `json.dumps(..., default=str)` per plan §Serialization; either satisfies the
`time`/`datetime`-not-serializable class of crash (research-2 mistake #1). Match whatever
LLMM-010 already imports.

## Acceptance criteria

- [ ] `OllamaAdapter.supports_ha_tools` is `True`; the `CONF_LLM_HASS_API` multi-select
      now appears in the Ollama conversation subentry form.
- [ ] Adapter sends a `tools` array when an LLM API is configured, and `None` otherwise.
- [ ] Native `message.tool_calls` chunks become `llm.ToolInput` deltas, with
      `_parse_tool_args` dropping `None`/`""` args and repairing stringified-JSON args.
- [ ] Prior `AssistantContent.tool_calls` and `ToolResultContent` are replayed into the
      Ollama `messages` array (tool-result serialized with `default=str`/`json_dumps`),
      and the LLMM-014 loop drives Ollama tool turns to completion.
- [ ] Text-only Ollama turns (no LLM API configured) are unchanged (LLMM-010 tests green).
- [ ] Gates green: `just check` + `just typecheck`.

## Verification

Write and run (`just test`) unit tests driving **raw NDJSON bytes** through the real
LLMM-010 reader + this adapter (split a JSON object across chunk boundaries; multiple
objects per chunk), asserting on `MockChatLog` content:

1. **Native tool_call extraction** — NDJSON stream where a message chunk carries
   `tool_calls` then a later chunk has `done: true`; assert one `llm.ToolInput` with the
   right name and args.
2. **Malformed-args repair** — a `tool_calls` chunk with args
   `{"area": "", "name": null, "domain": "light", "extra": "[1, 2]"}`; assert the emitted
   `tool_args == {"domain": "light", "extra": [1, 2]}` (empties dropped, stringified list
   parsed).
3. **Tool loop round-trip** — reuse the LLMM-014 loop test shape against the Ollama
   adapter: stream 1 yields a tool_call, MockChatLog appends `ToolResultContent`
   (carrying a `datetime` to prove `default=str` serialization), loop re-enters, stream 2
   yields text and breaks; assert 2 iterations and the replayed message list contains a
   `role: "tool"` message with the serialized result.
4. **Config-flow gating** — Ollama parent now offers `CONF_LLM_HASS_API`.

## Risks / open questions

- Ollama's `tool_args` dict shape is assumed (core treats it as a dict passed straight to
  `_parse_tool_args`). If a given server returns arguments as a JSON string instead,
  `_fix_invalid_arguments` on the whole value won't help — confirm against the owner's
  local ollama in LLMM-018; add a `json.loads`-on-str guard only if observed.
- Small-model tool reliability is inherently shaky; the repair mitigates but does not
  eliminate bad calls. The LLMM-014 iteration cap + never-hangs guard are the backstop —
  do not add Ollama-specific retry logic here.
