---
id: LLMM-010
title: Ollama native adapter (NDJSON, trim-history)
status: in-review
phase: 2
depends_on: [LLMM-003, LLMM-004]
---

# LLMM-010 — Ollama native adapter (NDJSON, trim-history)

## Context
The local-first default preset and the source of the NDJSON + trim-history patterns
(plan.md §Architecture `backends/ollama.py`, §Per-connector configuration matrix **Ollama
native** row, §Streaming parsers "Ollama NDJSON"). Ollama's `/api/chat` streams
**newline-delimited JSON** (not SSE), so it uses its own parser — hence this ticket does
**not** depend on `_sse.py` (LLMM-002). Stateless like OpenAI-compat, but **trims** history
via `_trim_history` (system + last `2*max_history+1`). Text-only in this ticket; native
`tool_calls` + malformed-arg repair land in **LLMM-015**.

## Scope
**In:**
- `custom_components/llm_middleman/backends/ollama.py` implementing the `BackendAdapter`
  ABC; `backend_type = "ollama"`; stateless replay + trim.
- `async_validate_connection`: `GET {base_url}/api/tags`; raise on failure; returns `None`.
- `async_list_models`: `GET {base_url}/api/tags`; return the installed-model list for the
  subentry model dropdown.
- `stream_turn(self, chat_log, user_input, ctx: TurnContext)`: stateless replay from
  `chat_log.content` → ollama `messages[]` (trimmed), `POST {base_url}/api/chat` with
  `stream: true`, parse the **NDJSON** response (line-per-JSON-object, `done: true`
  terminator), yield role-first `AssistantContentDeltaDict` deltas from `message.content`.
- Options read from `ctx.options`: `model`, `num_ctx`, `keep_alive`, `think` (the core-ollama
  option set) + `max_history` (trim). (Stateless replay ignores `ctx.memory_key`.)

**Out:**
- Native `tool_calls` parsing, `_parse_tool_args` malformed-arg repair, and flipping
  `supports_ha_tools = True` — **LLMM-015**. Set `supports_ha_tools = False` here (see
  Risks) so the subentry flow offers no dead tool option.
- SSE parsing — Ollama is NDJSON; no `_sse.py` usage.
- Subentry option schema + parent form — LLMM-006/LLMM-007.

## Implementation notes
- **Template:** HA core `homeassistant/components/ollama/entity.py` — `_convert_content`
  (one ChatLog item → one `ollama.Message`), `_trim_history`, and `_transform_stream`;
  `homeassistant/components/ollama/config_flow.py` for the option set. Consider the
  `ollama-python` client, but a direct `aiohttp` POST is fine and keeps deps thin
  (manifest lists no ollama dep today).
- **NDJSON parser (inside `ollama.py`, not `_sse.py`):** read `response.content` as raw
  bytes, maintain a byte buffer, split on `\n`, `json.loads` each **complete** line, keep
  the trailing partial fragment for the next chunk. Each object has `message.content`
  (text) and optionally `message.thinking`; the terminating object has `done: true`. Emit
  `{"role":"assistant"}` before the first non-empty content, then `{"content": chunk}` per
  object; stop on `done: true`. Do not strip whitespace. Handle split chunks / partial
  lines / a final object with no trailing newline.
- **History mapping** (research-4 table; text-only subset):
  - `SystemContent` → `Message(role="system", content=text)`
  - `UserContent` → `Message(role="user", content=text)`
  - `AssistantContent` → `Message(role="assistant", content=text, thinking=thinking_content)`
  - `ToolResultContent` → `Message(role="tool", content=json.dumps(result, default=str))`
    (won't appear until LLMM-015; map it so history needs no rework).
- **Trim:** `_trim_history(messages, max_history)` — keep `messages[0]` when it is the
  system message, plus the last `num_keep = 2*max_history + 1` messages; `max_history < 1`
  keeps everything. **Reuse the shared trim helper** created in LLMM-008
  (`backends/_history.py`) rather than forking a second copy.
- **Options → request:** `model` (top level), `options: {"num_ctx": …}`, `keep_alive`,
  `think` — send each only when configured. `base_url` is the **host root**, not `/v1`;
  `rstrip("/")` before appending `/api/chat` and `/api/tags`.
- **Auth:** usually none on LAN; send `Authorization` header when `CONF_API_KEY` is set
  (recent ollama config_flow supports it).
- **Tool seam (design, don't build):** keep the per-object emit loop able to also read
  `message.tool_calls` without restructuring; leave a `# tool_calls + _parse_tool_args:
  LLMM-015` marker. Emit nothing tool-related yet.
- **Const keys:** add `CONF_NUM_CTX`, `CONF_KEEP_ALIVE`, `CONF_THINK`,
  `BACKEND_OLLAMA = "ollama"` (reuse `CONF_MODEL`, `CONF_MAX_HISTORY`, `CONF_API_KEY`,
  `CONF_BASE_URL` from LLMM-008/006).

## Acceptance criteria
- [x] `OllamaAdapter(BackendAdapter)` with `backend_type = "ollama"`,
      `supports_ha_tools = False`, registered in `BACKEND_TO_CLS`.
- [x] `async_validate_connection` hits `GET /api/tags`, raises on failure, returns `None`.
- [x] `async_list_models` returns the installed-model list from `GET /api/tags`.
- [x] `stream_turn` replays trimmed history, POSTs `/api/chat` with `stream: true`, and
      streams `message.content` as role-first deltas.
- [x] NDJSON parser handles chunk-split lines, a partial trailing fragment, and terminates
      on `done: true` (and on EOF without a `done` object — guard guarantee holds).
- [x] `num_ctx`, `keep_alive`, `think`, `model` sent only when configured; `max_history`
      trims history to system + last `2*max_history+1`.
- [x] `base_url` treated as host root (no `/v1`), trailing slash stripped.
- [x] Gates green: `just check` + `just typecheck`.

## Verification
Write `tests/backends/test_ollama.py` driving **raw bytes** through the real NDJSON parser
(not pre-split lines — plan §Verification):
- **Happy path:** bytes `{"message":{"content":"Hel"},"done":false}\n{"message":{"content":"lo"},"done":false}\n{"message":{"content":""},"done":true}\n`
  with chunk boundaries **split mid-JSON-object and mid-line**. Assert deltas
  `[{"role":"assistant"},{"content":"Hel"},{"content":"lo"}]` and stream stops at
  `done: true`.
- **No trailing newline:** last object has no `\n` → still parsed and terminates.
- **EOF without `done`:** stream ends after a content object with no `done:true` →
  terminates; guard supplies the final `AssistantContent`.
- **Whitespace preserved:** a `" world"` content object is emitted verbatim.
- **Trim:** N-turn `chat_log.content`, `CONF_MAX_HISTORY=1` → provider `messages[]` =
  system + last 3.
- **Options:** `num_ctx`/`keep_alive`/`think` appear in the body only when set.
- **Validate:** fake `GET /api/tags` 200 → `async_validate_connection` returns `None`;
  connection error → raises. `async_list_models` on a 200 → returns the model list.
Run `just check` + `just typecheck`; record delta vs baseline.

### Verification evidence (executed)
`tests/backends/test_ollama.py` was written and drives raw bytes through the real
`_iter_ndjson` parser via the conftest harness (`chunk_bytes`, `FakeStreamResponse`).
All acceptance scenarios are covered by tests: happy path (byte-at-a-time,
mid-object-cut, and single-chunk splits), no-trailing-newline, EOF-without-done,
whitespace-preserved, done-with-no-delta, thinking role-first, malformed-JSON →
`BackendStreamError`, error-after-deltas propagation, non-200 → `BackendConnectionError`,
trim (system + last 3 at `max_history=1`), option gating (`num_ctx`/`keep_alive`/`think`
present only when set), `keep_alive=-1` sentinel stays literal int, base-url
trailing-slash strip, auth header, `/api/tags` validate/list (200/401/connection-error),
and registration. Gate results in the implementer worktree:

- `just lock-check` → `Resolved 214 packages` (clean).
- `just lint` → `All checks passed!`
- `just fmt-check` → `19 files already formatted`.
- `just typecheck` → `0 errors, 0 warnings, 0 notes`.
- `just test` → `80 passed` (baseline 55 + 25 new; no baseline test regressed —
  `test_backends_base.py::test_factory_empty_registry_raises` was repurposed to
  `test_factory_unknown_type_raises` since the registry is no longer empty once ollama
  registers).

## Risks / open questions
- **`supports_ha_tools = False` in Phase 2** is deliberate (anti-Potemkin) — LLMM-015 flips
  it to `True` alongside `tool_calls` parsing and `_parse_tool_args` repair.
- **Shared trim helper** must be the same code path as LLMM-008; if LLMM-008 hasn't landed
  the helper yet, coordinate so it exists in `backends/_history.py` for both.
- NDJSON parsing over `aiohttp` `response.content` must not use `.readline()` (v0's 64 KB
  limit `ValueError` defect); iterate raw chunks and split manually.
