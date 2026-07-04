---
id: LLMM-004
title: Test harness port (MockChatLog conftest + fake-stream helpers)
status: in-review
phase: 1
depends_on: []
---

# LLMM-004 — Test harness port (MockChatLog conftest + fake-stream helpers)

## Context
Implements the test-infrastructure line of `plan.md §Implementation phases` (phase 1:
"Port the MockChatLog conftest; per-adapter fake-stream test harness") and
`plan.md §Verification` ("Adapter unit tests drive fake aiohttp streams through the REAL
parser (raw bytes with split chunks/CRLF/multi-line data — **not** pre-split lines like
v0's `_FakeContent`)"). Every downstream test ticket (LLMM-005 entity guard, LLMM-008+
adapters, LLMM-006/007 flows) imports this harness. Getting the raw-byte fake and the
subentry-style config entry right here is what lets those tickets assert real behavior
instead of re-inventing scaffolding.

## Scope
**In:**
- `tests/conftest.py`, extended (the v0 file is the base):
  - **Port** `MockChatLog` (v0 `tests/conftest.py:40-59`) and the `mock_chat_log` fixture
    (v0 `:61-73`) — the HA-core `ChatLog`-subclass test pattern (confirmed current in
    research-1). Keep the names (`MockChatLog`, `mock_chat_log`) — LLMM-005 already imports
    `from .conftest import MockChatLog`. Fix them to pass **strict** pyright (see notes).
  - **Add** raw-byte fake-stream helpers (replacing v0's line-oriented fakes in
    `tests/test_conversation.py:26-64`, which pre-split lines):
    - `sse_bytes(*frames: tuple[str, str], newline: bytes = b"\n") -> bytes` — build a
      single SSE wire **blob** from `(event, data)` pairs (event line + data line(s) + blank
      line). `newline=b"\r\n"` exercises CRLF. Multi-line data supported via `"\n"` in the
      data string.
    - `chunk_bytes(blob: bytes, sizes: int | list[int]) -> list[bytes]` — split a blob at
      arbitrary boundaries (fixed width, `1` for byte-at-a-time, or explicit split offsets)
      so tests hit mid-line / mid-CRLF / mid-frame splits.
    - `FakeStreamResponse` — async-context-manager stand-in for `aiohttp.ClientResponse`:
      `status`, `headers`, `async text()`, `__aenter__/__aexit__`, and `.content` exposing
      `.iter_any()` that yields the exact provided byte chunks (no line re-splitting); it
      can raise a scripted exception after N chunks (to drive LLMM-005's guard paths:
      `ValueError`/`UnicodeDecodeError`/`TimeoutError`/`aiohttp.ClientError`).
    - `fake_aiohttp_session(*, response: FakeStreamResponse | None = None, exc: Exception |
      None = None)` — a `MagicMock` whose `.post(...)` returns the response (async CM) or
      raises `exc` (ports/generalizes v0 `_make_session`, `test_conversation.py:58-64`).
  - **Add** subentry-style config-entry fixtures (the v0 flat `mock_config_entry`,
    `:75-88`, is replaced):
    - `mock_config_entry` — a **parent** `MockConfigEntry` (`.data` = backend_type +
      base_url + api_key) carrying **one** `conversation` subentry built with
      `ConfigSubentryData(...)`; `.add_to_hass(hass)`.
    - a small factory (fixture or helper) to build an entry with a chosen `backend_type` and
      N conversation subentries, for LLMM-005 (`one entity per subentry`) and LLMM-006/007.
- `tests/backends/__init__.py` — empty package marker so `tests/backends/test_*.py`
  (LLMM-008+) is collectable.

**Out:**
- **Any adapter- or entity-specific test** (`test_openai_compat.py`, entity guard tests,
  flow tests) — owned by LLMM-005/006/007/008+. This ticket ships reusable harness only,
  plus a minimal smoke test proving the harness itself works.
- The `_sse.py` reader and its tests → LLMM-002 (this ticket must **not** import `_sse`; see
  Verification — it has no dependency on LLMM-002).
- The `BackendAdapter` fake used by LLMM-005 — LLMM-005 defines its own scripted fake
  adapter; this ticket provides the transport/chat-log/entry scaffolding it builds on.

## Implementation notes
- **`MockChatLog` port** (v0 `conftest.py:40-59`): a `@dataclass` subclass of
  `conversation.ChatLog` adding `_mock_tool_results` + `mock_tool_results()` and a
  read/write `llm_api` property. Under strict pyright this trips
  `reportIncompatibleVariableOverride` on `llm_api` (the base declares it as an attribute)
  and async-generator return-type errors on the fixtures. Resolve **properly**: annotate
  `_mock_tool_results: dict[str, Any]`, mirror the base's `llm_api` type in the
  getter/setter (read the installed base at
  `.venv/lib/python3.14/site-packages/homeassistant/components/conversation/chat_log.py`),
  and annotate the fixture return type `AsyncGenerator[MockChatLog]`. Prefer a correct
  annotation over a blanket `# pyright: ignore`; a single targeted, commented ignore is
  acceptable only if the base genuinely can't be matched.
- **`mock_chat_log` fixture** (v0 `:61-73`): keep the pattern that patches
  `homeassistant.components.conversation.chat_log.ChatLog` with `MockChatLog`, opens a
  `chat_session.async_get_chat_session(hass, "mock-conversation-id")`, and yields the
  `conversation.async_get_chat_log(hass, session)` ChatLog.
- **`FakeStreamResponse` contract**: adapters and `_sse` consume the stream via
  `response.content.iter_any()`, so that is the primary surface — implement `.content` as an
  object with an async `iter_any()` returning the chunks in order. Also give `.content` an
  `__aiter__` yielding the same chunks (harmless; documents that direct iteration is *not*
  the intended path). The scripted-exception hook raises **after** yielding K chunks so the
  guard's "error after ≥1 delta" path is reachable.
- **Subentry construction** — verified this session against the installed HA (2026.7.1):
  `from homeassistant.config_entries import ConfigSubentryData` is a `TypedDict` with fields
  `data: Mapping[str, Any]`, `subentry_type: str`, `title: str`, `unique_id: str | None`
  (`config_entries.py:336`). `MockConfigEntry(..., subentries_data=[ConfigSubentryData(
  data={...}, subentry_type="conversation", title="Test Agent", unique_id=None)])` is the
  supported constructor (phcc `common.py` accepts `subentries_data`). HA auto-assigns each
  `subentry_id`; read it back via `entry.subentries` (a `dict[str, ConfigSubentry]`).
- Keep helper values minimal and named per the testing-style rules — the reader's eye should
  land on the SSE bytes / subentry data under test, not scaffolding. Reuse the existing
  `TEST_URL`/token constants; add `TEST_BACKEND_TYPE`, `TEST_BASE_URL`, `TEST_API_KEY` as
  needed.

## Acceptance criteria
- [x] `MockChatLog` and `mock_chat_log` are ported (same names) and pass **strict** pyright.
- [x] `sse_bytes(...)` returns one bytes blob (event + data + blank line per frame; CRLF via
      `newline`), and `chunk_bytes(...)` splits a blob at arbitrary boundaries (incl. byte-
      at-a-time and mid-CRLF).
- [x] `FakeStreamResponse` is an async CM exposing `status`, `headers`, `text()`, and
      `.content.iter_any()` that yields the exact chunks given (no re-splitting); it can
      raise a scripted exception after K chunks.
- [x] `fake_aiohttp_session(response=…)` / `(exc=…)` returns the async-CM response from
      `.post` or raises the exception.
- [x] `mock_config_entry` is a parent `MockConfigEntry` with one `conversation` subentry
      (`ConfigSubentryData`), added to hass; the subentry-factory builds N subentries with a
      chosen `backend_type`.
- [x] No adapter/entity/flow test assertions live in this ticket (harness + one smoke test
      only).
- [x] Gates green: `just check` + `just typecheck`.

## Verification
Write `tests/test_harness_smoke.py` (proves the harness without depending on LLMM-002 —
this ticket has no dep on `_sse`):
- **chunk round-trip** — `blob = sse_bytes(("text_delta", '{"delta":"hi"}'))`;
  `b"".join(chunk_bytes(blob, 1)) == blob`; and collecting `FakeStreamResponse(blob-chunks)
  .content.iter_any()` reproduces `blob` exactly (proves no line re-splitting — the v0
  `_FakeContent` regression).
- **scripted exception** — a `FakeStreamResponse` set to raise `ValueError` after 1 chunk:
  iterating `.iter_any()` yields one chunk then raises `ValueError`.
- **session** — `fake_aiohttp_session(exc=TimeoutError()).post(...)` raises `TimeoutError`;
  with `response=` it returns an async CM whose `__aenter__` gives the response.
- **subentry entry** — `mock_config_entry` has exactly one `subentries` value with
  `subentry_type == "conversation"` and the expected `.data`; the factory yields 2
  subentries with distinct `subentry_id`s.
- **chat log** — inside `mock_chat_log`, appending assistant content via
  `async_add_assistant_content_without_tools(...)` lands in `chat_log.content` (confirms the
  fixture yields a usable real ChatLog).
Run `just check` + `just typecheck`; record the baseline failing set first (v0 tests may be
red under strict until LLMM-001/005/006/007 land) and report only this ticket's delta.

## Risks / open questions
- **Strict-pyright port of `MockChatLog` is the trickiest part.** The `llm_api` override is
  a real incompatibility against the base attribute; mirror the base's declared type exactly
  (read the installed `chat_log.py`) rather than suppressing. If HA declares `llm_api`
  read-only, the writable property may need a different mechanism — verify against source.
- **`ConfigSubentryData` / `subentries_data` shape is pinned to HA 2026.7.1** (verified this
  session). If the target HA floor differs, adjust field names; the fields are
  `data`/`subentry_type`/`title`/`unique_id`.
- **`FakeStreamResponse.content` dual surface** (`iter_any()` + `__aiter__`): document that
  adapters MUST use `.iter_any()` (bare `async for` on a real `aiohttp` `content` yields
  lines and bypasses `_sse`'s max-line guard — the v0 trap). The fake makes both work so a
  mistaken adapter still passes its own test but fails the intent; call this out so reviewers
  catch it.
- **Baseline coupling with LLMM-001.** If LLMM-004 lands before LLMM-001 flips pyright to
  strict, `just typecheck` here runs in *standard* mode; the ported `MockChatLog` must still
  be written to pass strict so it doesn't regress the gate once LLMM-001 merges. Verify with
  a local strict run (`basedpyright` against a temp strict config) before marking done.
