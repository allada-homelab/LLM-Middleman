"""Spec-compliant Server-Sent-Events reader (plus the NDJSON line framer).

Frames a raw byte stream (``AsyncIterable[bytes]`` of arbitrary chunks, e.g.
``response.content.iter_any()``) into ``ServerSentEvent`` objects following the
WHATWG "Server-sent events" stream/dispatch algorithm. This is pure transport:
bytes to ``(event, data)``. It knows nothing about JSON, ``[DONE]``, or terminal
event names -- per-preset delta extraction lives in the adapters.

Owning raw-byte framing here turns the two v0 failure modes into a typed,
catchable error: the oversized-line ``ValueError`` (aiohttp's readline cap) and
``UnicodeDecodeError`` (bad UTF-8) no longer escape. This module imports nothing
from ``base.py`` to keep the dependency direction one-way.

:func:`async_iter_lines` is the newline-delimited sibling for the NDJSON presets
(ollama, n8n), sharing the same per-line byte cap and skip-oversize strategy.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# One SSE line terminator: CRLF, a lone CR, or a lone LF (WHATWG). Matched in C so a
# chunk is split without a per-byte Python loop blocking the event loop.
_TERMINATOR = re.compile(rb"\r\n?|\n")

# Per-line buffering cap shared by both framers: bounds memory against a peer that
# never sends a terminator; an over-cap line is drained and skipped, never fatal.
MAX_LINE_BYTES = 65536


@dataclass(frozen=True)
class ServerSentEvent:
    """A dispatched SSE event.

    ``event`` defaults to ``"message"`` per spec; ``data`` is the accumulated,
    newline-joined payload of the frame's ``data:`` fields.
    """

    event: str
    data: str


class BackendStreamError(Exception):
    """Raised when a backend stream cannot be consumed.

    Adapters raise it to map a stream-level failure (e.g. a backend-sent ``error``
    frame) to the single fallback path. The framer no longer raises it on an
    oversized line -- it drains and skips the line instead (see ``async_iter_sse``).
    """


async def async_iter_sse(
    stream: AsyncIterable[bytes], *, max_line_bytes: int = MAX_LINE_BYTES
) -> AsyncGenerator[ServerSentEvent]:
    """Frame a raw byte stream into ``ServerSentEvent`` objects.

    ``stream`` yields arbitrary byte chunks whose boundaries need not align with
    lines, CRLFs, or events. Lines terminate on LF, CR, or CRLF (including a
    CRLF split across two chunks). Each completed line is decoded with
    ``errors="replace"`` so bad UTF-8 never raises. Consecutive ``data:`` fields
    accumulate and are joined with ``"\\n"``; the event dispatches on the blank
    line. A frame whose data buffer is empty (comment/keepalive-only or
    event-only) dispatches nothing.

    An unterminated line exceeding ``max_line_bytes`` is not buffered past the
    cap: the framer stops accumulating it, drains the remaining bytes up to the
    terminator, and skips the line (dispatching nothing) so the stream survives.
    A line over the cap is never a small streaming delta -- for a Dify
    advanced-chat app it is a verbose ``node_finished`` frame the adapter ignores
    -- so dropping one line beats aborting the whole turn. This bounds memory
    (the original guard's purpose) without making an oversized frame fatal.

    At EOF a trailing unterminated line is not flushed and a pending
    undispatched event is not emitted -- SSE requires a blank line to dispatch.
    """
    line_buf = bytearray()
    data_values: list[str] = []
    event_type = "message"
    prev_was_cr = False  # the previous chunk ended in CR: a leading LF here completes that CRLF
    line_overflowed = False  # current line passed the cap: drain to its terminator, then skip it

    def _handle_line(raw: bytes) -> ServerSentEvent | None:
        nonlocal data_values, event_type
        line = raw.decode("utf-8", errors="replace")
        if line == "":
            # Blank line -> dispatch. An empty data buffer dispatches nothing.
            event = ServerSentEvent(event=event_type, data="\n".join(data_values)) if data_values else None
            data_values = []
            event_type = "message"
            return event
        if line.startswith(":"):
            # Comment / keep-alive.
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_type = value
        elif field == "data":
            data_values.append(value)
        # id, retry, no-colon, and unknown fields are ignored.
        return None

    def _overflow() -> None:
        # Stop buffering (bound memory) and drain the rest of this line to its
        # terminator, then skip it -- an over-cap line is verbose node metadata,
        # never a content delta, so this beats aborting the turn.
        nonlocal line_overflowed
        _LOGGER.debug("SSE line exceeded %d bytes without a terminator; draining and skipping it", max_line_bytes)
        line_overflowed = True
        line_buf.clear()

    async for chunk in stream:
        if not chunk:
            continue
        pos = 0
        if prev_was_cr:
            prev_was_cr = False
            if chunk[0] == 0x0A:
                pos = 1  # trailing LF of a CRLF split across chunks; the CR ended the line
        for match in _TERMINATOR.finditer(chunk, pos):
            event = None
            if not line_overflowed:
                if line_buf:
                    line_buf += chunk[pos : match.start()]
                    raw = bytes(line_buf)
                else:
                    raw = chunk[pos : match.start()]
                if len(raw) > max_line_bytes:
                    _overflow()
                else:
                    event = _handle_line(raw)
            line_buf.clear()
            line_overflowed = False
            pos = match.end()
            # A CR closing the chunk may be the first half of a CRLF split across chunks.
            prev_was_cr = pos == len(chunk) and match.group() == b"\r"
            if event is not None:
                yield event
        if not line_overflowed and pos < len(chunk):
            line_buf += chunk[pos:]
            if len(line_buf) > max_line_bytes:
                _overflow()


async def async_iter_lines(
    stream: AsyncIterable[bytes], *, max_line_bytes: int = MAX_LINE_BYTES
) -> AsyncGenerator[bytes]:
    """Frame a raw byte stream into LF-terminated lines (NDJSON), without the ``\\n``.

    Chunk boundaries need not align with lines; a final unterminated line is flushed at
    EOF. Lines are yielded raw (blank lines included) so each caller keeps its own
    JSON/blank-line policy. A line longer than ``max_line_bytes`` is not buffered past
    the cap: it is logged, drained to its terminator, and skipped, so one oversized
    object cannot grow memory without bound or abort the stream.
    """
    line_buf = bytearray()
    line_overflowed = False  # current line passed the cap: drain to its terminator, then skip it

    def _skip_oversize() -> None:
        _LOGGER.warning("NDJSON line exceeded %d bytes; skipping it", max_line_bytes)

    async for chunk in stream:
        start = 0
        while (newline := chunk.find(b"\n", start)) != -1:
            if not line_overflowed:
                line_buf += chunk[start:newline]
                if len(line_buf) > max_line_bytes:
                    _skip_oversize()
                else:
                    yield bytes(line_buf)
            line_buf.clear()
            line_overflowed = False
            start = newline + 1
        if not line_overflowed:
            line_buf += chunk[start:]
            if len(line_buf) > max_line_bytes:
                _skip_oversize()
                line_overflowed = True
                line_buf.clear()
    if line_buf:
        yield bytes(line_buf)
