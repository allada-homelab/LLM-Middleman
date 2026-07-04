"""Test fixtures and reusable stream/transport harness for LLM Middleman.

The v1 test suite drives fake aiohttp streams through the *real* parsers, so the
fakes here yield **raw bytes at arbitrary chunk boundaries** (never pre-split
lines — the v0 `_FakeContent` trap). Downstream tickets (adapters, entity guard,
flows) import this harness instead of re-inventing scaffolding.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import chat_session, llm
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_middleman.const import (
    CONF_NAME,
    CONF_SYSTEM_PROMPT,
    DOMAIN,
)

TEST_URL = "http://middleman.local:8000"
TEST_TOKEN = "test-token"

# v1 parent-entry connection fields. The string keys mirror the plan's per-connector
# matrix; the CONF_* constants for them land with the foundation ticket, so the
# harness uses literals here rather than depending on symbols that don't exist yet.
TEST_BACKEND_TYPE = "openai_compat"
TEST_BASE_URL = "http://backend.local:8000"
TEST_API_KEY = "test-api-key"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Automatically enable custom integrations in all tests."""


@pytest.fixture(autouse=True)
async def setup_ha_components(hass: HomeAssistant) -> None:
    """Set up required HA components for integration tests."""
    from homeassistant.setup import async_setup_component

    await async_setup_component(hass, "homeassistant", {})
    await hass.async_block_till_done()


@dataclass
class MockChatLog(conversation.ChatLog):
    """Mock ChatLog that allows controlling tool results (HA core test pattern)."""

    _mock_tool_results: dict[str, Any] = field(default_factory=dict)

    def mock_tool_results(self, results: dict[str, Any]) -> None:
        """Set mock tool results."""
        self._mock_tool_results = results

    # The base declares `llm_api` as a plain dataclass field; a property override is
    # inherently unassignable to a variable in pyright, so this one incompatibility is
    # suppressed at the single unavoidable spot. The getter/setter mirror the base's
    # exact type, so no type safety is lost.
    @property
    def llm_api(self) -> llm.APIInstance | None:
        """Return the LLM API."""
        return self._llm_api

    @llm_api.setter
    def llm_api(self, value: llm.APIInstance | None) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Set the LLM API."""
        self._llm_api = value


@pytest.fixture
async def mock_chat_log(hass: HomeAssistant) -> AsyncGenerator[MockChatLog]:
    """Yield a real (mocked-class) chat log within a chat session."""
    with (
        patch(
            "homeassistant.components.conversation.chat_log.ChatLog",
            MockChatLog,
        ),
        chat_session.async_get_chat_session(hass, "mock-conversation-id") as session,
        # chat_log_delta_listener's dict param is untyped upstream in HA.
        conversation.async_get_chat_log(  # pyright: ignore[reportUnknownMemberType]
            hass, session
        ) as chat_log,
    ):
        # The ChatLog patch above makes this hold at runtime; narrows the type.
        assert isinstance(chat_log, MockChatLog)
        yield chat_log


def sse_bytes(*frames: tuple[str, str], newline: bytes = b"\n") -> bytes:
    r"""Build one SSE wire blob from ``(event, data)`` pairs.

    Each frame emits an ``event:`` line, one ``data:`` line per line of the data
    string (multi-line data via ``"\n"``), then a blank separator line.
    ``newline=b"\r\n"`` exercises CRLF framing.
    """
    out = bytearray()
    for event, data in frames:
        out += b"event: " + event.encode() + newline
        for data_line in data.split("\n"):
            out += b"data: " + data_line.encode() + newline
        out += newline
    return bytes(out)


def chunk_bytes(blob: bytes, sizes: int | list[int]) -> list[bytes]:
    """Split ``blob`` at arbitrary boundaries for stream-parser tests.

    ``sizes`` as an ``int`` produces fixed-width chunks (``1`` = byte-at-a-time); as
    a ``list[int]`` it is a list of ascending absolute split offsets, so a test can
    cut mid-line or mid-CRLF. Empty chunks are dropped (a real stream never yields
    empty ``bytes``); joining the result always reproduces ``blob``.
    """
    if isinstance(sizes, int):
        if sizes < 1:
            raise ValueError("chunk width must be >= 1")
        return [blob[i : i + sizes] for i in range(0, len(blob), sizes)]
    chunks: list[bytes] = []
    prev = 0
    for offset in sizes:
        chunks.append(blob[prev:offset])
        prev = offset
    chunks.append(blob[prev:])
    return [chunk for chunk in chunks if chunk]


class _FakeStreamContent:
    """Stand-in for ``aiohttp.ClientResponse.content``.

    ``iter_any()`` is the intended consumption surface (mirrors how adapters and
    ``_sse`` read the stream): it yields the exact provided chunks with no line
    re-splitting. ``__aiter__`` reuses the same generator so a mistaken adapter that
    iterates ``content`` directly still runs — documenting (per the plan) that
    ``iter_any()`` is the contract, not bare ``async for``.
    """

    def __init__(
        self,
        chunks: list[bytes],
        *,
        raise_after: int | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._raise_after = raise_after
        self._exc = exc

    async def iter_any(self) -> AsyncIterator[bytes]:
        """Yield the exact chunks, raising the scripted exception after N of them."""
        for index, chunk in enumerate(self._chunks):
            if self._exc is not None and self._raise_after is not None and index >= self._raise_after:
                raise self._exc
            yield chunk
        if self._exc is not None and self._raise_after is not None and self._raise_after >= len(self._chunks):
            raise self._exc

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self.iter_any()


class FakeStreamResponse:
    """Async-context-manager stand-in for ``aiohttp.ClientResponse``.

    Exposes ``status``, ``headers``, an async ``text()``, and ``.content`` whose
    ``iter_any()`` yields the exact byte chunks given. A scripted exception can be
    raised *after* ``raise_after`` chunks so the entity guard's "error after ≥1
    delta" path is reachable.
    """

    def __init__(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        text: str = "",
        raise_after: int | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.status = status
        self.headers: Mapping[str, str] = headers if headers is not None else {}
        self._text = text
        self.content = _FakeStreamContent(chunks, raise_after=raise_after, exc=exc)

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> FakeStreamResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def fake_aiohttp_session(
    *,
    response: FakeStreamResponse | None = None,
    exc: Exception | None = None,
) -> MagicMock:
    """Return a session mock whose ``.post`` yields ``response`` or raises ``exc``."""
    session = MagicMock()
    if exc is not None:
        session.post = MagicMock(side_effect=exc)
    else:
        session.post = MagicMock(return_value=response)
    return session


def build_config_entry(
    hass: HomeAssistant,
    *,
    backend_type: str = TEST_BACKEND_TYPE,
    subentry_count: int = 1,
) -> MockConfigEntry:
    """Build a parent entry (chosen ``backend_type``) with N conversation subentries.

    The entry is added to ``hass``; HA auto-assigns each subentry a distinct
    ``subentry_id``. Powers the "one entity per subentry" tests downstream.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Middleman",
        data={
            "backend_type": backend_type,
            "base_url": TEST_BASE_URL,
            "api_key": TEST_API_KEY,
        },
        subentries_data=[
            ConfigSubentryData(
                data={CONF_NAME: f"Agent {index}", CONF_SYSTEM_PROMPT: ""},
                subentry_type="conversation",
                title=f"Agent {index}",
                unique_id=None,
            )
            for index in range(subentry_count)
        ],
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a parent entry with one conversation subentry, added to hass."""
    return build_config_entry(hass)
