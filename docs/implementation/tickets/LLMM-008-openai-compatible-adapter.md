---
id: LLMM-008
title: OpenAI-compatible adapter (text-only)
status: in-review
phase: 1
depends_on: [LLMM-002, LLMM-003, LLMM-004]
---

# LLMM-008 — OpenAI-compatible adapter (text-only)

## Context
First concrete `BackendAdapter` and the default preset — one adapter unlocks OpenAI,
vLLM, LocalAI, LM Studio, llama.cpp-server, Ollama's `/v1` shim, OpenRouter, Groq,
together, etc. Implements plan.md §Architecture (`backends/openai_compat.py`),
§Per-connector configuration matrix (**OpenAI-compatible** row), §Streaming parsers
(`choices[].delta.content`, `[DONE]` sentinel), and the stateless-replay half of the
adapter interface (§Adapter interface docstring). This ticket delivers **text-only**
streaming; the tool loop (tool-schema passing + `tool_calls` fragment accumulation) is
owned by **LLMM-014** — this ticket only shapes the seam so LLMM-014 drops in without
reshaping `stream_turn`.

## Scope
**In:**
- `custom_components/llm_middleman/backends/openai_compat.py` implementing the
  `BackendAdapter` ABC from LLMM-003.
- `backend_type = "openai_compat"` classvar; registered in `BACKEND_TO_CLS`.
- `async_validate_connection`: `GET {base_url}/v1/models` with Bearer auth; raise on
  non-200/transport failure; returns `None`.
- `async_list_models`: `GET {base_url}/v1/models`; parse and return the model-id list so the
  subentry flow (LLMM-007) can populate its model dropdown.
- `stream_turn`: stateless full-history replay from `chat_log.content` → OpenAI
  `messages[]` (trimmed to `CONF_MAX_HISTORY`), `POST {base_url}/v1/chat/completions`
  with `stream: true`, parsed through the shared `_sse.py` reader (LLMM-002), yielding
  canonical `AssistantContentDeltaDict` deltas.
- Text delta extraction: `choices[0].delta.content`; `[DONE]` sentinel terminates the
  stream; first emitted delta of the block carries `{"role": "assistant"}`.
- `stream_turn(self, chat_log, user_input, ctx: TurnContext)` — request options read from
  `ctx.options`: `model`, `temperature`, `top_p`, `max_tokens` (the core-openai option set)
  — sent only when set. (Stateless replay ignores `ctx.memory_key`.)
- Base-URL trailing-slash strip and dummy-key handling (see Implementation notes).

**Out:**
- HA tool loop / `tool_calls` execution / tool-schema formatting — **LLMM-014**. Set
  `supports_ha_tools = False` here (see Risks) so the subentry flow does not offer a
  tool option that does nothing; LLMM-014 flips it to `True`.
- The subentry option schema (temperature/top_p/max_tokens/model form fields) — LLMM-007
  owns the flow; this ticket only *reads* `ctx.options`.
- Parent connection form + `base_url`/`api_key` collection — LLMM-006.

## Implementation notes
- **Template:** HA core `homeassistant/components/openai_conversation/entity.py` —
  `_convert_content_to_param` (message mapping) and `_transform_stream` (delta yield
  shape). Core targets the newer Responses API; use the simpler **chat/completions**
  wire format (`choices[].delta.content` / `data: [DONE]`), not Responses events.
- **History replay mapping** (research-4 table; text-only subset for now):
  - `SystemContent` → `{"role": "system", "content": text}`
  - `UserContent` → `{"role": "user", "content": text}`
  - `AssistantContent` → `{"role": "assistant", "content": text}`
  - `ToolResultContent` → `{"role": "tool", "tool_call_id": …, "content": json.dumps(result, default=str)}`
    (won't appear until tools exist, but map it so LLMM-014 needs no history change).
- **Trim:** unlike core-openai's untruncated replay, apply the ollama-style trim keyed on
  `CONF_MAX_HISTORY`: keep `content[0]` if it is the system message + the last
  `2*max_history+1` messages; `max_history < 1` keeps everything. Factor this into a small
  shared helper (e.g. `backends/_history.py` `trim_history(messages, max_history)`) so
  LLMM-010 (Ollama) reuses the identical logic rather than duplicating it.
- **Streaming:** feed `response.content` (raw bytes) to the `_sse.py` reader; for each
  parsed `data:` payload: if the payload string is exactly `[DONE]`, stop; else
  `json.loads` it and read `choices[0].delta.content`. Emit `{"role": "assistant"}` once
  before the first non-empty content delta, then `{"content": delta}` per fragment. **Do
  not trim whitespace** off deltas (research-2 footgun). Empty-string deltas pass through
  untrimmed per plan §never-hangs guard.
- **Auth:** `Authorization: Bearer <api_key>`. `api_key` is optional in the config entry;
  many "compatible" servers still require a **non-empty dummy value** — the parent flow
  (LLMM-006) hints this; the adapter simply sends the header when a key is present.
- **Gotcha — trailing slash:** `base_url` with a trailing `/` double-slashes the path and
  404s `/models` and `/chat/completions`. The parent flow normalizes (strips) it; the
  adapter should also `rstrip("/")` defensively before concatenating (v0 pattern:
  `conversation.py:122`).
- **Tool seam (design, don't build):** keep `stream_turn`'s stream-consumption loop able
  to receive `choices[].delta.tool_calls` fragments (accumulated by `index`) without a
  structural change — i.e. branch on delta keys, don't hardcode "content only". Leave a
  single `# tool_calls: LLMM-014` marker. Pass no tool schemas yet
  (`chat_log.llm_api` is `None` until Phase 3).
- **Const keys** (add to `const.py` if LLMM-006/007 haven't yet): `CONF_TEMPERATURE`,
  `CONF_TOP_P`, `CONF_MAX_TOKENS`, `CONF_MAX_HISTORY`, `CONF_MODEL`, `CONF_API_KEY`,
  `CONF_BASE_URL`, `BACKEND_OPENAI_COMPAT = "openai_compat"`.

## Acceptance criteria
- [x] `OpenAICompatAdapter(BackendAdapter)` exists with `backend_type = "openai_compat"`,
      `supports_ha_tools = False`, and is registered in `BACKEND_TO_CLS`.
- [x] `async_validate_connection` hits `GET /v1/models`, raises the adapter's typed error
      on failure, and returns `None` on success.
- [x] `async_list_models` returns the parsed model-id list from `GET /v1/models`.
- [x] `stream_turn` replays trimmed history to `messages[]`, POSTs
      `/v1/chat/completions` with `stream: true`, and streams `choices[0].delta.content`
      as role-first `AssistantContentDeltaDict` deltas.
- [x] `[DONE]` sentinel ends the stream cleanly; a stream that ends without `[DONE]` still
      terminates (EOF) and the guard's ≥1-`AssistantContent` guarantee holds.
- [x] `temperature`, `top_p`, `max_tokens`, `model` from `ctx.options` appear in the
      request body only when configured.
- [x] `base_url` trailing slash is stripped; Bearer header sent only when `api_key` set.
- [x] Gates green: `just check` + `just typecheck`.

## Verification
Write `tests/backends/test_openai_compat.py` driving **raw bytes** through the real
`_sse.py` + adapter (per plan §Verification — not pre-split lines):
- **Happy path:** a byte stream of `data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n`
  … `data: {"choices":[{"delta":{"content":"lo"}}]}\n\n` … `data: [DONE]\n\n`, with the
  chunk boundaries **split mid-line and mid-`data:` frame** and using `\r\n`. Assert the
  collected deltas are `[{"role":"assistant"}, {"content":"Hel"}, {"content":"lo"}]`.
- **Role-first:** assert the first yielded delta carries `role`.
- **No-`[DONE]` EOF:** stream ends after a content delta without the sentinel → still
  terminates; final `AssistantContent` equals the concatenated text.
- **Whitespace preserved:** a delta `" world"` is emitted verbatim (not stripped).
- **Trim:** build a `chat_log.content` of N turns, set `CONF_MAX_HISTORY=1`, assert the
  provider `messages[]` = system + last 3.
- **Options:** assert `temperature`/`top_p`/`max_tokens` land in the body only when set.
- **Validate:** fake `GET /v1/models` 200 → `async_validate_connection` returns `None`;
  401/500 → raises. `async_list_models` on a 200 → returns the parsed model list.
Run `just check` + `just typecheck` and record the pass/fail delta vs baseline.

### Executed evidence (this branch)
All commands run in the worktree via `uv run`:
- `just check` (lock-check + lint + fmt-check + tests): **76 passed** (baseline 55 +
  21 new in `tests/backends/test_openai_compat.py`); ruff `All checks passed!`; ruff
  format `19 files already formatted`.
- `just typecheck` (basedpyright strict): **0 errors, 0 warnings, 0 notes**.
- `uv lock --check`: clean (`Resolved 214 packages`).

The 21 new tests drive raw bytes through the real `_sse` reader + adapter:
- Happy path — byte-at-a-time / CRLF split mid-line & mid-frame → deltas
  `[{"role":"assistant"},{"content":"Hel"},{"content":"lo"}]` (role-first asserted).
- Whitespace preserved (`" world"` verbatim); empty-string delta passes through.
- Fallback surfaces: done-with-no-delta (yields nothing), silent stream end (no
  `[DONE]` EOF still terminates), error-after-deltas (`ClientError` propagates after
  partial deltas), oversized line (`BackendStreamError`), malformed JSON
  (`json.JSONDecodeError`).
- History trim (`max_history=1` → system + last 3) and untrimmed (`max_history=0`).
- Options present only when set / omitted when unset; `stream: true` always.
- Trailing-slash strip + Bearer header present with key / absent without key.
- Registration in `BACKEND_TO_CLS`; validate 200→None, 401→`BackendAuthError`,
  500 & transport→`BackendConnectionError`; `async_list_models` parses ids.

## Risks / open questions
- **`supports_ha_tools = False` in Phase 1** is deliberate anti-Potemkin: the flag gates
  the subentry's `llm_hass_api` option, and executing tools isn't wired until LLMM-014.
  LLMM-014 must flip it to `True` in the same PR that lands tool execution.
- **Shared trim helper location:** if LLMM-003's `base.py` already exposes a history
  helper, use it; otherwise create `backends/_history.py`. Don't fork two copies with
  LLMM-010.
- Plan §Implementation-time checkpoints: confirm the `_transform_stream` /
  `async_add_delta_content_stream` delta shape against the pinned HA source before
  finalizing (the entity wiring in LLMM-005 owns the loop; this adapter must yield exactly
  the dict keys that loop consumes).
