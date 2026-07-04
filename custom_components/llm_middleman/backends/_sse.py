"""Spec-compliant Server-Sent Events reader (shared).

SHIM — placeholder for the real module delivered by LLMM-002. It exists only so
LLMM-003's ``base.py`` can import and re-export ``BackendStreamError`` and so the
package typechecks in isolation. The merge that lands LLMM-002 replaces this file
with the real implementation; do not extend it here.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass

__all__ = ["BackendStreamError", "ServerSentEvent", "async_iter_sse"]


class BackendStreamError(Exception):
    """Raised when a backend byte stream cannot be framed into events."""


@dataclass(frozen=True)
class ServerSentEvent:
    """One dispatched SSE event."""

    event: str
    data: str


async def async_iter_sse(
    stream: AsyncIterable[bytes],
    *,
    max_line_bytes: int = 65536,
) -> AsyncGenerator[ServerSentEvent]:
    """Frame raw byte chunks into SSE events (minimal shim implementation).

    Accumulates ``data:`` lines and dispatches on a blank line. The real,
    fully spec-compliant reader (CRLF, comments, oversized-line handling) ships
    in LLMM-002 and replaces this shim.
    """
    buffer = b""
    data_values: list[str] = []
    event_type = "message"
    async for chunk in stream:
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            line = raw.rstrip(b"\r")
            if len(line) > max_line_bytes:
                raise BackendStreamError("SSE line exceeded max_line_bytes")
            if not line:
                if data_values:
                    yield ServerSentEvent(event=event_type, data="\n".join(data_values))
                data_values = []
                event_type = "message"
                continue
            if line.startswith(b":"):
                continue
            field, _, value = line.partition(b":")
            text = value.decode("utf-8", errors="replace")
            if text.startswith(" "):
                text = text[1:]
            if field == b"data":
                data_values.append(text)
            elif field == b"event":
                event_type = text
