---
id: LLMM-002
title: Spec-compliant SSE reader (`backends/_sse.py`) + raw-byte test harness
status: done
phase: 1
depends_on: []
---

# LLMM-002 — Spec-compliant SSE reader (`backends/_sse.py`) + raw-byte test harness

## Context
Implements the `_sse.py` half of `plan.md §Streaming parsers`. This is the shared,
backend-agnostic Server-Sent-Events reader every SSE preset (OpenAI-compatible /
LangGraph / custom converse) parses its stream through. It exists to design out the v0
defects catalogued in `plan.md §Verified constraints`: v0 parsed SSE **per line**
(`conversation.py:158-199`) instead of buffering `data:` fields and dispatching on the
blank line, and let `ValueError` (aiohttp's 64 KB readline cap) and `UnicodeDecodeError`
**escape** its `except (TimeoutError, ClientError)` — breaking the never-hangs guarantee.
This reader owns raw-byte framing so those failure modes become a typed, catchable error.

Reference for correctness: the WHATWG "Server-Sent Events" stream/dispatch algorithm.

## Scope
**In:**
- `custom_components/llm_middleman/backends/__init__.py` — create as an **empty** package
  marker so `backends._sse` is importable. (LLMM-003 populates it with `BACKEND_TO_CLS`;
  whichever ticket lands first creates the file, the other edits it.)
- `custom_components/llm_middleman/backends/_sse.py` with:
  - `ServerSentEvent` — a small frozen dataclass `(event: str, data: str)` (`event`
    defaults to `"message"` per spec; `data` is the accumulated, newline-joined payload).
  - `BackendStreamError(Exception)` — raised when the byte stream can't be framed (the
    oversized-line case). LLMM-005's guard catches it broadly and maps it to the fallback;
    LLMM-003's `base.py` re-exports it for the unified backends exception surface.
  - `async def async_iter_sse(stream: AsyncIterable[bytes], *, max_line_bytes: int = 65536)
    -> AsyncGenerator[ServerSentEvent]` — frames raw byte chunks into events.
- `tests/test_sse.py` — the **raw-byte** test harness (self-contained; see Verification).

**Out:**
- Any per-preset delta extraction (`choices[].delta.content`, `[DONE]` sentinel,
  `message.content`, messages-tuple filtering, `text_delta`/`done`/`error` events) — those
  live in the adapters (LLMM-008/009/011). This reader is pure transport: bytes →
  `(event, data)`. It knows nothing about JSON, `[DONE]`, or terminal event names.
- NDJSON parsing (Ollama `/api/chat`, n8n StructuredChunk) — NOT SSE; handled inside
  `ollama.py`/`n8n.py` (LLMM-010/012).
- The richer fake-aiohttp `ClientResponse` helper and shared fixtures → **LLMM-004**. This
  ticket's harness is a trivial local async byte generator (no aiohttp, no conftest dep,
  since LLMM-002 depends on nothing).

## Implementation notes
**Input contract.** `async_iter_sse` consumes an `AsyncIterable[bytes]` of **arbitrary**
chunks (not lines). Adapters feed it `response.content.iter_any()` — **never** bare
`async for line in response.content`, which yields aiohttp-split lines and reintroduces the
64 KB `ValueError` this reader exists to own. State this in the adapter tickets' usage; the
reader must not assume chunk boundaries align with lines, CRLFs, or events.

**Framing algorithm** (process the byte stream, not decoded text, so newline handling is
chunk-boundary-safe):
- Maintain a `bytearray` line buffer, a `list[str]` of accumulated `data` values, an
  `event_type` string (default `"message"`), and a `prev_was_cr` flag.
- Walk incoming bytes. A line terminates on `LF (0x0A)`, `CR (0x0D)`, or `CRLF`. Handle a
  CRLF split across two chunks: after a CR ends a line, if the very next byte is LF, drop
  it (use `prev_was_cr`). Lone CR and lone LF each also terminate a line (spec requires all
  three).
- Enforce `max_line_bytes` on the **unterminated** line buffer: if it exceeds the cap
  before a terminator arrives, `raise BackendStreamError(...)` (this replaces aiohttp's
  escaping `ValueError`).
- On each completed line, decode with `bytes(buf).decode("utf-8", errors="replace")`
  (never raise on bad UTF-8 — v0's `UnicodeDecodeError` hole), then:
  - `""` (blank line) → **dispatch**: if `data` values are non-empty, `yield
    ServerSentEvent(event=event_type, data="\n".join(data_values))`; then reset
    `data_values=[]` and `event_type="message"`. Per spec, an **empty** data buffer
    dispatches **nothing** (comment/keepalive-only or event-only frames are dropped).
  - starts with `":"` → comment/keep-alive, ignore.
  - else split on the first `":"` (`field, _, value = line.partition(":")`); if `value`
    starts with a single space, strip exactly one. `field == "event"` → set `event_type`;
    `field == "data"` → append `value`; any other field (`id`, `retry`, no-colon lines,
    unknown) → ignore.
- **EOF**: when the input iterator is exhausted, do **not** flush a trailing unterminated
  line and do **not** dispatch a pending (undispatched) event — SSE requires a blank line
  to dispatch. (Backends that end a stream by simply closing the socket are handled by the
  adapter/guard via EOF, not by this reader inventing an event.)

Byte-by-byte iteration is fine — voice payloads are small; do **not** use
`bytes.splitlines()` (it splits on `\v`/`\f`/unicode separators too). `ServerSentEvent`
and `BackendStreamError` are the only public names besides `async_iter_sse`.

## Acceptance criteria
- [x] `ServerSentEvent(event, data)`, `BackendStreamError`, and `async_iter_sse(stream, *,
      max_line_bytes=65536)` exist with the signatures above; `_sse.py` imports nothing from
      `base.py` (no cycle).
- [x] Consecutive `data:` lines accumulate and are joined with `\n`; the event dispatches on
      the blank line; an empty data buffer dispatches nothing.
- [x] `LF`, `CR`, and `CRLF` line endings all frame correctly, **including a `CRLF` split
      across two chunks** and a stream fed one byte at a time.
- [x] `:`-prefixed comment lines and non-`event`/`data` fields are ignored; a single leading
      space after `data:` is stripped (two spaces → value keeps one).
- [x] Invalid UTF-8 bytes are replaced (no exception); a single line exceeding
      `max_line_bytes` raises `BackendStreamError`.
- [x] A stream that ends mid-event (no trailing blank line) dispatches nothing for the
      partial event and does not raise.
- [x] Gates green: `just check` + `just typecheck`.

## Verification
Write `tests/test_sse.py`. Drive **raw bytes** through the real `async_iter_sse` — never
pre-split lines (`plan.md §Verification`). Helper:
```python
async def _chunks(*parts: bytes) -> AsyncGenerator[bytes]:
    for p in parts:
        yield p
```
Collect with `events = [e async for e in async_iter_sse(_chunks(*parts))]`. Cases:
- **single event** — `b"data: hello\n\n"` → one `ServerSentEvent("message", "hello")`.
- **named event + JSON** — `b"event: text_delta\ndata: {\"delta\":\"hi\"}\n\n"` → event
  `"text_delta"`, data `'{"delta":"hi"}'` (reader does NOT parse JSON).
- **multi-line data** — `b"data: a\ndata: b\n\n"` → data `"a\nb"`.
- **CRLF** — same input with `\r\n` terminators → identical result.
- **split mid-line** — feed the single-event bytes **one byte per chunk**; identical result.
- **CRLF split across chunks** — split so `\r` ends one chunk and `\n` starts the next;
  assert exactly one line break (no phantom blank line / premature dispatch).
- **comment ignored** — `b":keep-alive\n"` interleaved produces no event.
- **leading-space strip** — `b"data:  x\n\n"` → data `" x"` (one of two spaces removed).
- **decode replace** — an invalid UTF-8 byte in a data line yields a replacement char, no
  raise.
- **oversized line** — a data line longer than a small `max_line_bytes` (pass e.g. `16`)
  raises `BackendStreamError` (`pytest.raises`).
- **empty-buffer no dispatch** — `b"\n\n"` (blank lines only) yields nothing.
- **EOF mid-event** — `b"data: partial\n"` (no blank line) yields nothing, no raise.
Run `just test` (or `just check`) + `just typecheck`; record baseline failing set, report
the delta.

## Risks / open questions
- **Data-less terminal events.** Per spec, a frame like `event: end` with **no** `data:`
  line dispatches nothing. Adapters that expect a data-less terminator must instead detect
  EOF. This intersects the LangGraph checkpoint (`plan.md §Implementation-time checkpoints`:
  verify `messages-tuple` frame shape + terminal `end`/`error` event names against a live
  `langgraph dev` capture) — flagged for LLMM-011, not resolved here. If a real backend is
  found to signal completion via a data-less event, revisit whether the reader should also
  surface event-only frames.
- **`max_line_bytes` default (65536).** Mirrors aiohttp's historical readline cap. Legit
  single-line JSON deltas are small, so 64 KB is generous; it is a per-call kwarg so an
  adapter with unusually large frames can raise it. Confirm no target backend emits a
  legitimate single line over the cap.
- **`BackendStreamError` ownership.** Defined here (raised here); LLMM-003 `base.py`
  re-exports it so adapters import backend exceptions from one place. Keep the import
  direction one-way (`base` imports from `_sse`, never the reverse).
