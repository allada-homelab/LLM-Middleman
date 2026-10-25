"""Test fixtures and reusable stream/transport harness for LLM Middleman.

The v1 test suite drives fake aiohttp streams through the *real* parsers, so the
fakes here yield **raw bytes at arbitrary chunk boundaries** (never pre-split
lines — the v0 `_FakeContent` trap). Downstream tickets (adapters, entity guard,
flows) import this harness instead of re-inventing scaffolding.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol
from aiohttp import web
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry, ConfigSubentryData
from homeassistant.const import CONF_PROMPT
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import chat_session, llm
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.llm_middleman.backends.base import BackendAdapter
from custom_components.llm_middleman.const import (
    CONF_NAME,
    DOMAIN,
)
from custom_components.llm_middleman.conversation import LLMMiddlemanConversationEntity
from tests.live_backend import PRESETS, ScriptedBackend

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


# HA 2026.10 wrapped tool payloads in ``llm.ToolResult`` (``ToolResultContent.result``,
# returned by ``async_call_tool``); older HA used the bare JSON dict (``tool_result``).
HA_HAS_TOOL_RESULT_OBJ = "result" in {f.name for f in fields(conversation.ToolResultContent)}


def tool_call_result(data: Any) -> Any:
    """Return ``data`` in the shape ``llm.APIInstance.async_call_tool`` returns on this HA."""
    return llm.ToolResult(data=data) if HA_HAS_TOOL_RESULT_OBJ else data


def tool_result_content(
    *, agent_id: str, tool_call_id: str, tool_name: str, data: Any, error: bool = False
) -> conversation.ToolResultContent:
    """Build a ``ToolResultContent`` carrying ``data`` on both HA tool-result APIs.

    ``error`` only exists on HA >= 2026.10 (``llm.ToolResult.error``); older HA drops it.
    """
    if HA_HAS_TOOL_RESULT_OBJ:
        return conversation.ToolResultContent(
            agent_id=agent_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            result=llm.ToolResult(data=data, error=error),
        )
    # The pre-2026.10 signature; the type-checked (latest) HA no longer has it.
    return conversation.ToolResultContent(  # pyright: ignore[reportCallIssue]
        agent_id=agent_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_result=data,  # pyright: ignore[reportCallIssue]
    )


def external_tool_history() -> list[conversation.Content]:
    """A chat log mixing backend-run (``external``) tool calls with one HA-run call.

    Mirrors switching from a LangGraph/Dify agent (which records its backend's tool
    activity as external calls) to a stateless-replay agent mid-conversation. Only
    ``call_1`` (HA-run) and its result may be replayed.
    """
    return [
        conversation.SystemContent(content="sys"),
        conversation.UserContent(content="weather?"),
        conversation.AssistantContent(
            agent_id="other",
            tool_calls=[llm.ToolInput(id="ext_1", tool_name="web_search", tool_args={"q": "w"}, external=True)],
        ),
        tool_result_content(agent_id="other", tool_call_id="ext_1", tool_name="web_search", data={"r": "sunny"}),
        conversation.AssistantContent(agent_id="other", content="It is sunny."),
        conversation.UserContent(content="light on"),
        conversation.AssistantContent(
            agent_id="a",
            content="Sure",
            tool_calls=[
                llm.ToolInput(id="ext_2", tool_name="lookup", tool_args={}, external=True),
                llm.ToolInput(id="call_1", tool_name="HassTurnOn", tool_args={"name": "light"}),
            ],
        ),
        tool_result_content(agent_id="a", tool_call_id="ext_2", tool_name="lookup", data={"r": "x"}),
        tool_result_content(agent_id="a", tool_call_id="call_1", tool_name="HassTurnOn", data={"ok": True}),
    ]


# JSON Schema properties an HA-style tool schema (cv validator + coerce/range, as the
# Assist API builds) must render to, whichever converter the running HA needs.
TOOL_SCHEMA_PROPERTIES = {
    "name": {"type": "string"},
    "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
}


def vol_tool_schema() -> vol.Schema:
    """HA-style tool parameters via ``voluptuous`` (probatio's shim on HA >= 2026.9)."""
    return vol.Schema(
        {
            vol.Required("name"): cv.string,
            vol.Optional("brightness"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        }
    )


def probatio_tool_schema() -> Any:
    """The same tool parameters as a native ``probatio.Schema``; skips on HA < 2026.9."""
    probatio = pytest.importorskip("probatio")
    return probatio.Schema(
        {
            probatio.Required("name"): str,
            probatio.Optional("brightness"): probatio.All(probatio.Coerce(int), probatio.Range(min=0, max=100)),
        }
    )


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


_FIXTURES = Path(__file__).parent / "backends" / "fixtures"


def capture_response(preset: str, name: str, *, chunk_size: int = 7) -> FakeStreamResponse:
    """Replay ``fixtures/<preset>/<name>.json`` (status + exact body) as a fake response.

    The body streams in ``chunk_size``-byte chunks so frame boundaries fall mid-line;
    error statuses also expose it through ``text()``.
    """
    capture = json.loads((_FIXTURES / preset / f"{name}.json").read_text())
    body: str = capture["body"]
    return FakeStreamResponse(chunk_bytes(body.encode(), chunk_size), status=capture["status"], text=body)


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


class HangingStreamContent:
    """Stream content that yields ``first`` and then stalls until cancelled (a run in flight)."""

    def __init__(self, first: bytes) -> None:
        self._first = first
        self.reached = asyncio.Event()

    async def iter_any(self) -> AsyncIterator[bytes]:
        yield self._first
        self.reached.set()
        await asyncio.Event().wait()


class FakeJsonResponse:
    """Async-CM stand-in for a JSON ``GET``/``POST`` probe response: ``status``, ``json()``, ``text()``.

    ``payload`` defaults to an empty object; an exception payload is raised from ``json()``
    (an undecodable body). ``raw`` overrides ``text()`` (default: the JSON-encoded payload).
    """

    _EMPTY: Any = object()

    def __init__(self, payload: object = _EMPTY, *, status: int = 200, raw: str | None = None) -> None:
        self.status = status
        self._payload: object = {} if payload is FakeJsonResponse._EMPTY else payload
        self._raw = raw

    async def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def text(self) -> str:
        return self._raw if self._raw is not None else json.dumps(self._payload)

    async def __aenter__(self) -> FakeJsonResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def fake_get_session(*, response: FakeJsonResponse | None = None, exc: Exception | None = None) -> MagicMock:
    """Return a session mock whose ``.get`` yields ``response`` or raises ``exc``."""
    session = MagicMock()
    if exc is not None:
        session.get = MagicMock(side_effect=exc)
    else:
        session.get = MagicMock(return_value=response)
    return session


def make_input(text: str = "hi", *, user_id: str | None = None, **fields: Any) -> conversation.ConversationInput:
    """A ``ConversationInput`` for adapter tests; ``fields`` override any other constructor argument."""
    values: dict[str, Any] = {
        "context": Context(user_id=user_id),
        "conversation_id": "conv-1",
        "device_id": None,
        "satellite_id": None,
        "language": "en",
        "agent_id": "agent-1",
        **fields,
    }
    return conversation.ConversationInput(text=text, **values)


async def collect(
    stream: AsyncIterable[conversation.AssistantContentDeltaDict],
) -> list[conversation.AssistantContentDeltaDict]:
    """Drain an adapter's delta stream into a list."""
    return [delta async for delta in stream]


def text_of(deltas: list[conversation.AssistantContentDeltaDict]) -> str:
    """Concatenate the ``content`` of a delta list (the text that would be spoken)."""
    return "".join(delta.get("content") or "" for delta in deltas)


class FakeTool:
    """Minimal duck-typed ``llm.Tool`` (name/description/voluptuous parameters)."""

    name = "get_time"
    description: str | None = "Return the current time"
    parameters: Any = vol.Schema({})


class UndescribedTool(FakeTool):
    """A tool with no description (``None``, not empty)."""

    description = None


class FakeLLMApi:
    """Duck-typed ``llm.APIInstance`` exposing only what adapters and ``ChatLog`` read.

    ``tools`` defaults to one :class:`FakeTool`; ``async_call_tool`` returns ``result``.
    """

    def __init__(
        self,
        tools: list[Any] | None = None,
        result: Any = None,
        custom_serializer: Callable[[Any], Any] | None = None,
    ) -> None:
        self.tools: list[Any] = tools if tools is not None else [FakeTool()]
        self.custom_serializer = custom_serializer
        self._result: Any = result if result is not None else {}

    async def async_call_tool(self, tool_input: llm.ToolInput) -> Any:
        return tool_call_result(self._result)


def build_entity(
    hass: HomeAssistant, adapter: BackendAdapter, subentry_data: Mapping[str, Any]
) -> LLMMiddlemanConversationEntity:
    """Build a conversation entity driving a real ``adapter`` with ``subentry_data`` options.

    Lets adapter tests run the entity's real ``async_provide_llm_data`` path, so the
    agent prompt reaches the adapter the same way it does in production.
    """
    entry = MagicMock()
    entry.runtime_data = adapter
    subentry = MagicMock(spec=ConfigSubentry)
    subentry.subentry_id = "sub-1"
    subentry.title = "Agent"
    subentry.data = dict(subentry_data)
    entity = LLMMiddlemanConversationEntity(entry, subentry)
    entity.entity_id = "conversation.test"
    entity.hass = hass
    return entity


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
        # Entries created by the current flow are VERSION 2 (config_flow.py); v0
        # version-1 entries are exercised by the LLMM-013 migration tests instead.
        version=2,
        data={
            "backend_type": backend_type,
            "base_url": TEST_BASE_URL,
            "api_key": TEST_API_KEY,
        },
        subentries_data=[
            ConfigSubentryData(
                data={CONF_NAME: f"Agent {index}", CONF_PROMPT: ""},
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


@pytest.fixture(params=PRESETS)
def preset(request: pytest.FixtureRequest) -> str:
    """Parametrize a test over every backend preset registered in ``BACKEND_TO_CLS``."""
    return str(request.param)


@pytest.fixture
async def live_backend(socket_enabled: None) -> AsyncGenerator[ScriptedBackend]:
    """Yield a real scripted backend server listening on an ephemeral 127.0.0.1 port."""
    backend = ScriptedBackend()
    runner = web.AppRunner(backend.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    backend.base_url = f"http://127.0.0.1:{site.port}"
    yield backend
    await runner.cleanup()
