"""Smoke tests proving the LLMM-004 test harness itself works.

No dependency on ``_sse`` (LLMM-002) — this exercises only the fake transport,
chat-log, and subentry-config scaffolding the downstream test tickets build on.
"""

from __future__ import annotations

import pytest
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant

from custom_components.llm_middleman.const import CONF_NAME

from .conftest import (
    FakeStreamResponse,
    MockChatLog,
    build_config_entry,
    chunk_bytes,
    fake_aiohttp_session,
    sse_bytes,
)


async def _drain(response: FakeStreamResponse) -> bytes:
    return b"".join([chunk async for chunk in response.content.iter_any()])


def test_sse_bytes_crlf_and_multiline() -> None:
    """CRLF newline and multi-line data render one data line per input line."""
    blob = sse_bytes(("text_delta", "a\nb"), newline=b"\r\n")
    assert blob == b"event: text_delta\r\ndata: a\r\ndata: b\r\n\r\n"


async def test_chunk_and_stream_preserve_raw_bytes() -> None:
    """Byte-at-a-time chunking and the fake stream reproduce the blob verbatim.

    This is the v0 `_FakeContent` regression guard: the fake must NOT re-split on
    lines — the exact bytes handed in come back out.
    """
    blob = sse_bytes(("text_delta", '{"delta":"hi"}'))

    assert b"".join(chunk_bytes(blob, 1)) == blob

    response = FakeStreamResponse(chunk_bytes(blob, 1))
    assert await _drain(response) == blob


def test_chunk_bytes_splits_mid_crlf() -> None:
    """Explicit offsets cut inside the trailing CRLF; join still reproduces the blob."""
    blob = sse_bytes(("done", "{}"), newline=b"\r\n")
    parts = chunk_bytes(blob, [len(blob) - 1])

    assert len(parts) == 2
    assert parts[-1] == b"\n"
    assert b"".join(parts) == blob


async def test_stream_raises_scripted_exception_after_chunks() -> None:
    """A stream set to raise after 1 chunk yields exactly one chunk, then raises."""
    chunks = chunk_bytes(sse_bytes(("text_delta", '{"delta":"hi"}'), ("done", "{}")), 8)
    response = FakeStreamResponse(chunks, raise_after=1, exc=ValueError("boom"))

    seen: list[bytes] = []
    with pytest.raises(ValueError, match="boom"):
        async for chunk in response.content.iter_any():
            seen.append(chunk)

    assert seen == chunks[:1]


def test_fake_session_raises_configured_exception() -> None:
    """`fake_aiohttp_session(exc=...)` raises that exception from `.post`."""
    session = fake_aiohttp_session(exc=TimeoutError())
    with pytest.raises(TimeoutError):
        session.post("http://backend.local/v1/chat/completions")


async def test_fake_session_returns_async_cm_response() -> None:
    """`fake_aiohttp_session(response=...)` returns an async CM yielding the response."""
    response = FakeStreamResponse(chunk_bytes(sse_bytes(("done", "{}")), 4))
    session = fake_aiohttp_session(response=response)

    async with session.post("http://backend.local/v1/chat/completions") as resp:
        assert resp is response
        assert resp.status == 200


def test_mock_config_entry_has_one_conversation_subentry(
    mock_config_entry: object,
) -> None:
    """The parent entry carries exactly one `conversation` subentry with its data."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    assert isinstance(mock_config_entry, MockConfigEntry)
    subentries = list(mock_config_entry.subentries.values())
    assert len(subentries) == 1
    assert subentries[0].subentry_type == "conversation"
    assert subentries[0].data[CONF_NAME] == "Agent 0"


def test_factory_builds_distinct_subentries(hass: HomeAssistant) -> None:
    """The factory yields N subentries with distinct auto-assigned subentry_ids."""
    entry = build_config_entry(hass, subentry_count=2)
    subentries = list(entry.subentries.values())

    assert len(subentries) == 2
    assert len({sub.subentry_id for sub in subentries}) == 2


async def test_mock_chat_log_accepts_assistant_content(
    mock_chat_log: MockChatLog,
) -> None:
    """The fixture yields a usable real ChatLog that records assistant content."""
    mock_chat_log.async_add_assistant_content_without_tools(
        conversation.AssistantContent(agent_id="conversation.test", content="hello")
    )

    last = mock_chat_log.content[-1]
    assert isinstance(last, conversation.AssistantContent)
    assert last.content == "hello"
