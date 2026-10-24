"""Raw-byte tests for the spec-compliant SSE reader.

Drives the real ``async_iter_sse`` with arbitrary byte chunks through a trivial
local async generator -- never pre-split lines (v0's ``_FakeContent`` laxity).
Self-contained: no conftest fixtures, no aiohttp.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

import pytest

from custom_components.llm_middleman.backends._sse import (
    ServerSentEvent,
    async_iter_lines,
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


async def test_lone_cr_terminators() -> None:
    events = await _collect(b"data: a\rdata: b\r\r")
    assert events == [ServerSentEvent("message", "a\nb")]


async def test_cr_ending_a_chunk_without_lf_still_ends_the_line() -> None:
    # A CR closing a chunk is a full terminator when the next chunk doesn't start with LF.
    events = await _collect(b"data: a\r", b"data: b\r", b"\r")
    assert events == [ServerSentEvent("message", "a\nb")]


async def test_empty_chunk_between_split_crlf() -> None:
    events = await _collect(b"data: a\r", b"", b"\n\n")
    assert events == [ServerSentEvent("message", "a")]


_MIXED = b"event: x\r\ndata: 1\r\rdata:  2\n:c\r\ndata: \xc3\xa9\n\r\ndata: z\r\n\r\n"


@pytest.mark.parametrize("cut", range(1, len(_MIXED)))
async def test_any_two_chunk_split_frames_like_the_whole(cut: int) -> None:
    whole = await _collect(_MIXED)
    assert whole == [ServerSentEvent("x", "1"), ServerSentEvent("message", " 2\né"), ServerSentEvent("message", "z")]
    assert await _collect(_MIXED[:cut], _MIXED[cut:]) == whole


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


async def test_oversized_line_drained_and_skipped() -> None:
    # An unterminated line over the cap is drained to its terminator and skipped
    # (dispatches nothing) instead of raising -- the stream survives.
    events = await _collect(b"data: " + b"x" * 64 + b"\n\n", max_line_bytes=16)
    assert events == []


async def test_oversized_line_then_next_event_still_frames() -> None:
    # After skipping an oversized line, a following well-formed event frames
    # normally -- the real reason to drain rather than abort (a Dify chatflow's
    # huge node_finished frame precedes the answer deltas).
    events = await _collect(b"data: " + b"x" * 64 + b"\ndata: ok\n\n", max_line_bytes=16)
    assert events == [ServerSentEvent("message", "ok")]


async def test_oversized_line_drained_across_chunks() -> None:
    # The drain must span chunk boundaries: one byte per chunk, oversized line
    # then a clean event.
    payload = b"data: " + b"x" * 64 + b"\ndata: ok\n\n"
    events = await _collect(*(payload[i : i + 1] for i in range(len(payload))), max_line_bytes=16)
    assert events == [ServerSentEvent("message", "ok")]


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


# --- NDJSON line framer ------------------------------------------------------


async def _lines(*parts: bytes, max_line_bytes: int = 65536) -> list[bytes]:
    return [line async for line in async_iter_lines(_chunks(*parts), max_line_bytes=max_line_bytes)]


async def test_lines_split_across_chunks_with_eof_tail() -> None:
    lines = await _lines(b'{"a":', b"1}\n\n{", b'"b":2}')
    assert lines == [b'{"a":1}', b"", b'{"b":2}']


async def test_lines_one_byte_per_chunk() -> None:
    blob = b"ab\ncd\n"
    assert await _lines(*(blob[i : i + 1] for i in range(len(blob)))) == [b"ab", b"cd"]


async def test_lines_oversize_line_across_chunks_skipped_next_delivered(caplog: pytest.LogCaptureFixture) -> None:
    """An over-cap line is never buffered whole: drained to its LF, logged, skipped."""
    with caplog.at_level(logging.WARNING):
        lines = await _lines(b"ok\n" + b"x" * 6, b"x" * 6, b"xx\nnext\n", max_line_bytes=8)
    assert lines == [b"ok", b"next"]
    assert "exceeded 8 bytes" in caplog.text


async def test_lines_oversize_line_within_one_chunk_skipped() -> None:
    assert await _lines(b"x" * 20 + b"\nnext\n", max_line_bytes=8) == [b"next"]


async def test_lines_oversize_unterminated_tail_not_flushed() -> None:
    assert await _lines(b"ok\n", b"x" * 20, max_line_bytes=8) == [b"ok"]


async def test_lines_line_at_cap_delivered() -> None:
    assert await _lines(b"x" * 8 + b"\n", max_line_bytes=8) == [b"x" * 8]
