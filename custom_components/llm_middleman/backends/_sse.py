"""Spec-compliant Server-Sent-Events reader.

Frames a raw byte stream (``AsyncIterable[bytes]`` of arbitrary chunks, e.g.
``response.content.iter_any()``) into ``ServerSentEvent`` objects following the
WHATWG "Server-sent events" stream/dispatch algorithm. This is pure transport:
bytes to ``(event, data)``. It knows nothing about JSON, ``[DONE]``, or terminal
event names -- per-preset delta extraction lives in the adapters.

Owning raw-byte framing here turns the two v0 failure modes into a typed,
catchable error: the oversized-line ``ValueError`` (aiohttp's readline cap) and
``UnicodeDecodeError`` (bad UTF-8) no longer escape. This module imports nothing
from ``base.py`` to keep the dependency direction one-way.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass

_CR = 0x0D
_LF = 0x0A


@dataclass(frozen=True)
class ServerSentEvent:
    """A dispatched SSE event.

    ``event`` defaults to ``"message"`` per spec; ``data`` is the accumulated,
    newline-joined payload of the frame's ``data:`` fields.
    """

    event: str
    data: str


class BackendStreamError(Exception):
    """Raised when the byte stream cannot be framed (oversized line).

    Replaces the ``ValueError`` aiohttp raises when a line exceeds its readline
    cap, so callers can catch a single typed error and map it to the fallback.
    """


async def async_iter_sse(
    stream: AsyncIterable[bytes], *, max_line_bytes: int = 65536
) -> AsyncGenerator[ServerSentEvent]:
    """Frame a raw byte stream into ``ServerSentEvent`` objects.

    ``stream`` yields arbitrary byte chunks whose boundaries need not align with
    lines, CRLFs, or events. Lines terminate on LF, CR, or CRLF (including a
    CRLF split across two chunks). Each completed line is decoded with
    ``errors="replace"`` so bad UTF-8 never raises. Consecutive ``data:`` fields
    accumulate and are joined with ``"\\n"``; the event dispatches on the blank
    line. A frame whose data buffer is empty (comment/keepalive-only or
    event-only) dispatches nothing. An unterminated line exceeding
    ``max_line_bytes`` raises ``BackendStreamError``.

    At EOF a trailing unterminated line is not flushed and a pending
    undispatched event is not emitted -- SSE requires a blank line to dispatch.
    """
    line_buf = bytearray()
    data_values: list[str] = []
    event_type = "message"
    prev_was_cr = False

    def _handle_line() -> ServerSentEvent | None:
        nonlocal data_values, event_type
        line = bytes(line_buf).decode("utf-8", errors="replace")
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

    async for chunk in stream:
        for byte in chunk:
            if prev_was_cr:
                prev_was_cr = False
                if byte == _LF:
                    # Trailing LF of a CRLF; the CR already terminated the line.
                    continue
            if byte == _CR:
                event = _handle_line()
                line_buf.clear()
                prev_was_cr = True
                if event is not None:
                    yield event
                continue
            if byte == _LF:
                event = _handle_line()
                line_buf.clear()
                if event is not None:
                    yield event
                continue
            line_buf.append(byte)
            if len(line_buf) > max_line_bytes:
                raise BackendStreamError(f"SSE line exceeded {max_line_bytes} bytes without a terminator")
