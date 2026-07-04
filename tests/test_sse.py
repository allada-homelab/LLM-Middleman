"""Raw-byte tests for the spec-compliant SSE reader.

Drives the real ``async_iter_sse`` with arbitrary byte chunks through a trivial
local async generator -- never pre-split lines (v0's ``_FakeContent`` laxity).
Self-contained: no conftest fixtures, no aiohttp.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from custom_components.llm_middleman.backends._sse import (
    BackendStreamError,
    ServerSentEvent,
    async_iter_sse,
)


async def _chunks(*parts: bytes) -> AsyncGenerator[bytes]:
    for part in parts:
        yield part


async def _collect(*parts: bytes, max_line_bytes: int = 65536) -> list[ServerSentEvent]:
    return [event async for event in async_iter_sse(_chunks(*parts), max_line_bytes=max_line_bytes)]


async def test_single_event() -> None:
    events = await _collect(b"data: hello\n\n")
    assert events == [ServerSentEvent("message", "hello")]


async def test_named_event_with_json_not_parsed() -> None:
    events = await _collect(b'event: text_delta\ndata: {"delta":"hi"}\n\n')
    assert events == [ServerSentEvent("text_delta", '{"delta":"hi"}')]


async def test_multi_line_data_joined_with_newline() -> None:
    events = await _collect(b"data: a\ndata: b\n\n")
    assert events == [ServerSentEvent("message", "a\nb")]


async def test_crlf_terminators_match_lf() -> None:
    events = await _collect(b"data: a\r\ndata: b\r\n\r\n")
    assert events == [ServerSentEvent("message", "a\nb")]


async def test_one_byte_per_chunk() -> None:
    payload = b"data: hello\n\n"
    events = await _collect(*(payload[i : i + 1] for i in range(len(payload))))
    assert events == [ServerSentEvent("message", "hello")]


async def test_crlf_split_across_chunks() -> None:
    # "\r" ends one chunk, "\n" starts the next: exactly one line break, no
    # phantom blank line / premature dispatch.
    events = await _collect(b"data: a\r", b"\ndata: b\r", b"\n\r", b"\n")
    assert events == [ServerSentEvent("message", "a\nb")]


async def test_comment_line_ignored() -> None:
    events = await _collect(b":keep-alive\ndata: x\n\n")
    assert events == [ServerSentEvent("message", "x")]


async def test_comment_only_frame_produces_nothing() -> None:
    events = await _collect(b":keep-alive\n\n")
    assert events == []


async def test_leading_space_strips_exactly_one() -> None:
    events = await _collect(b"data:  x\n\n")
    assert events == [ServerSentEvent("message", " x")]


async def test_no_leading_space_preserved() -> None:
    events = await _collect(b"data:x\n\n")
    assert events == [ServerSentEvent("message", "x")]


async def test_invalid_utf8_replaced_no_raise() -> None:
    events = await _collect(b"data: \xff\n\n")
    assert events == [ServerSentEvent("message", "�")]


async def test_oversized_line_raises() -> None:
    with pytest.raises(BackendStreamError):
        await _collect(b"data: " + b"x" * 64 + b"\n\n", max_line_bytes=16)


async def test_line_at_cap_does_not_raise() -> None:
    # A line of exactly max_line_bytes must frame without raising.
    events = await _collect(b"data:aaaaaaaaaa\n\n", max_line_bytes=15)
    assert events == [ServerSentEvent("message", "aaaaaaaaaa")]


async def test_empty_buffer_no_dispatch() -> None:
    events = await _collect(b"\n\n")
    assert events == []


async def test_event_only_frame_no_dispatch() -> None:
    events = await _collect(b"event: end\n\n")
    assert events == []


async def test_eof_mid_event_yields_nothing() -> None:
    events = await _collect(b"data: partial\n")
    assert events == []


async def test_non_data_event_fields_ignored() -> None:
    events = await _collect(b"id: 1\nretry: 5000\ndata: y\n\n")
    assert events == [ServerSentEvent("message", "y")]


async def test_event_type_resets_between_frames() -> None:
    events = await _collect(b"event: text_delta\ndata: one\n\ndata: two\n\n")
    assert events == [
        ServerSentEvent("text_delta", "one"),
        ServerSentEvent("message", "two"),
    ]
