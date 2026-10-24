"""Raw-byte tests for the OpenAI-compatible adapter (LLMM-008).

Drives the *real* ``_sse`` reader + adapter with fake aiohttp streams whose chunk
boundaries fall mid-line and mid-frame (byte-at-a-time / arbitrary offsets, CRLF),
never pre-split lines. Covers the plan's fallback surfaces: done-with-no-delta,
silent stream end, error-after-deltas, oversized line, malformed JSON.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
import voluptuous as vol
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.llm_middleman.backends import BACKEND_TO_CLS, get_backend_cls
from custom_components.llm_middleman.backends.base import (
    BackendAuthError,
    BackendConnectionError,
    BackendStreamError,
    TurnContext,
)
from custom_components.llm_middleman.backends.openai_compat import OpenAICompatAdapter
from custom_components.llm_middleman.const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MAX_HISTORY,
    CONF_MAX_TOKENS,
    CONF_MODEL,
    CONF_TEMPERATURE,
    CONF_TOP_P,
    IDLE_TIMEOUT,
)
from tests.conftest import (
    HA_HAS_TOOL_RESULT_OBJ,
    TEST_API_KEY,
    TEST_BASE_URL,
    TOOL_SCHEMA_PROPERTIES,
    FakeStreamResponse,
    MockChatLog,
    chunk_bytes,
    fake_aiohttp_session,
    probatio_tool_schema,
    tool_call_result,
    tool_result_content,
    vol_tool_schema,
)

_CONNECTION = {CONF_BASE_URL: TEST_BASE_URL, CONF_API_KEY: TEST_API_KEY}


def _data_frame(delta: dict[str, Any]) -> bytes:
    """One SSE ``data:`` frame carrying an OpenAI chat-completions chunk, CRLF-framed."""
    payload = json.dumps({"choices": [{"delta": delta}]})
    return b"data: " + payload.encode() + b"\r\n\r\n"


def _input() -> conversation.ConversationInput:
    return conversation.ConversationInput(
        text="hi",
        context=Context(),
        conversation_id="conv-1",
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="agent-1",
    )


def _adapter(hass: HomeAssistant, session: Any) -> OpenAICompatAdapter:
    return OpenAICompatAdapter(hass, session, _CONNECTION)


async def _collect(
    hass: HomeAssistant,
    chat_log: conversation.ChatLog,
    chunks: list[bytes],
    ctx: TurnContext,
    *,
    raise_after: int | None = None,
    exc: Exception | None = None,
) -> list[conversation.AssistantContentDeltaDict]:
    response = FakeStreamResponse(chunks, raise_after=raise_after, exc=exc)
    adapter = _adapter(hass, fake_aiohttp_session(response=response))
    return [delta async for delta in adapter.stream_turn(chat_log, _input(), ctx)]


def _ctx(**options: Any) -> TurnContext:
    return TurnContext(options=options, memory_key="k")


# --- streaming happy path -------------------------------------------------------


async def test_happy_path_role_first_split_mid_frame(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # role-only chunk (content is None) is skipped; adapter emits its own role delta.
    blob = (
        _data_frame({"role": "assistant"})
        + _data_frame({"content": "Hel"})
        + _data_frame({"content": "lo"})
        + b"data: [DONE]\r\n\r\n"
    )
    # byte-at-a-time: boundaries fall mid-line and mid-`data:` frame.
    deltas = await _collect(hass, mock_chat_log, chunk_bytes(blob, 1), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "Hel"}, {"content": "lo"}]
    assert deltas[0] == {"role": "assistant"}  # role-first


async def test_whitespace_preserved(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    blob = _data_frame({"content": "Hello"}) + _data_frame({"content": " world"}) + b"data: [DONE]\n\n"
    deltas = await _collect(hass, mock_chat_log, [blob], _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "Hello"}, {"content": " world"}]


async def test_empty_string_delta_passes_through(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    blob = _data_frame({"content": ""}) + _data_frame({"content": "x"}) + b"data: [DONE]\n\n"
    deltas = await _collect(hass, mock_chat_log, [blob], _ctx())
    assert deltas == [{"role": "assistant"}, {"content": ""}, {"content": "x"}]


# --- fallback surfaces ----------------------------------------------------------


async def test_no_done_eof_terminates(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # silent stream end: content deltas but no [DONE] sentinel — EOF still ends it.
    blob = _data_frame({"content": "Hel"}) + _data_frame({"content": "lo"})
    deltas = await _collect(hass, mock_chat_log, chunk_bytes(blob, 1), _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "Hel"}, {"content": "lo"}]
    text = "".join(str(d.get("content", "")) for d in deltas if d.get("content"))
    assert text == "Hello"


async def test_done_with_no_delta_yields_nothing(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    blob = _data_frame({}) + b"data: [DONE]\n\n"
    deltas = await _collect(hass, mock_chat_log, [blob], _ctx())
    assert deltas == []


async def test_error_after_deltas_propagates(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    chunks = [_data_frame({"content": "Hi"}), _data_frame({"content": "!"})]
    adapter = _adapter(
        hass, fake_aiohttp_session(response=FakeStreamResponse(chunks, raise_after=1, exc=aiohttp.ClientError()))
    )
    deltas: list[conversation.AssistantContentDeltaDict] = []
    with pytest.raises(aiohttp.ClientError):
        async for delta in adapter.stream_turn(mock_chat_log, _input(), _ctx()):
            deltas.append(delta)
    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]


async def test_oversized_line_drained_then_stream_continues(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # A data line beyond the reader's cap is drained and skipped (not fatal); a
    # following valid delta still streams instead of the whole turn aborting.
    blob = b"data: " + b"x" * 70000 + b"\n\n" + _data_frame({"content": "Hi"}) + b"data: [DONE]\n\n"
    deltas = await _collect(hass, mock_chat_log, [blob], _ctx())
    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]


async def test_malformed_json_raises(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    with pytest.raises(json.JSONDecodeError):
        await _collect(hass, mock_chat_log, [b"data: not-json\n\n"], _ctx())


@pytest.mark.parametrize("status", [401, 500])
async def test_http_error_logs_and_raises(
    hass: HomeAssistant, mock_chat_log: MockChatLog, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    body = '{"error": {"message": "Incorrect API key provided"}}'
    response = FakeStreamResponse([], status=status, text=body)
    adapter = _adapter(hass, fake_aiohttp_session(response=response))
    with caplog.at_level("ERROR"), pytest.raises(BackendStreamError):
        _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx())]
    assert f"HTTP {status}" in caplog.text
    assert "Incorrect API key provided" in caplog.text
    assert TEST_API_KEY not in caplog.text


# --- history replay + options ---------------------------------------------------


def _posted_body(session: MagicMock) -> dict[str, Any]:
    return session.post.call_args.kwargs["json"]


async def test_history_trim_system_plus_last_three(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    content: list[conversation.Content] = [
        conversation.SystemContent(content="sys"),
        conversation.UserContent(content="u1"),
        conversation.AssistantContent(agent_id="a", content="a1"),
        conversation.UserContent(content="u2"),
        conversation.AssistantContent(agent_id="a", content="a2"),
        conversation.UserContent(content="u3"),
    ]
    mock_chat_log.content = content
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = _adapter(hass, session)
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx(**{CONF_MAX_HISTORY: 1}))]
    messages = _posted_body(session)["messages"]
    assert messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]


async def test_history_untrimmed_when_max_history_zero(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    content: list[conversation.Content] = [
        conversation.SystemContent(content="sys"),
        conversation.UserContent(content="u1"),
        conversation.AssistantContent(agent_id="a", content="a1"),
        conversation.UserContent(content="u2"),
    ]
    mock_chat_log.content = content
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = _adapter(hass, session)
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx())]
    assert len(_posted_body(session)["messages"]) == 4


async def test_options_present_only_when_set(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = _adapter(hass, session)
    ctx = _ctx(**{CONF_MODEL: "m", CONF_TEMPERATURE: 0.5, CONF_TOP_P: 0.9, CONF_MAX_TOKENS: 100})
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), ctx)]
    body = _posted_body(session)
    assert body["model"] == "m"
    assert body["temperature"] == 0.5
    assert body["top_p"] == 0.9
    assert body["max_tokens"] == 100
    assert body["stream"] is True


async def test_options_omitted_when_unset(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = _adapter(hass, session)
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx(**{CONF_MODEL: "m"}))]
    body = _posted_body(session)
    assert body["model"] == "m"
    assert "temperature" not in body
    assert "top_p" not in body
    assert "max_tokens" not in body


async def test_trailing_slash_and_bearer_header(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = OpenAICompatAdapter(hass, session, {CONF_BASE_URL: TEST_BASE_URL + "/", CONF_API_KEY: "sekret"})
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx())]
    assert session.post.call_args.args[0] == f"{TEST_BASE_URL}/v1/chat/completions"
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer sekret"


async def test_no_bearer_header_without_api_key(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = OpenAICompatAdapter(hass, session, {CONF_BASE_URL: TEST_BASE_URL})
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx())]
    assert "Authorization" not in session.post.call_args.kwargs["headers"]


# --- tools (LLMM-014) -----------------------------------------------------------


class _FakeTool:
    """Minimal duck-typed ``llm.Tool`` (name/description/voluptuous parameters)."""

    name = "get_time"
    description = "Return the current time"
    parameters = vol.Schema({})


class _FakeLLMApi:
    """Duck-typed ``llm.APIInstance`` exposing only what the adapter/chat_log read."""

    def __init__(
        self,
        tools: list[Any],
        result: dict[str, Any] | None = None,
        custom_serializer: Callable[[Any], Any] | None = None,
    ) -> None:
        self.tools = tools
        self.custom_serializer = custom_serializer
        self._result: dict[str, Any] = result if result is not None else {}

    async def async_call_tool(self, tool_input: llm.ToolInput) -> Any:
        return tool_call_result(self._result)


def _tc_frame(call: dict[str, Any]) -> bytes:
    """One SSE frame carrying a single ``delta.tool_calls`` fragment, CRLF-framed."""
    payload = json.dumps({"choices": [{"delta": {"tool_calls": [call]}}]})
    return b"data: " + payload.encode() + b"\r\n\r\n"


async def test_tool_calls_reassembled_across_chunks(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # Two interleaved calls whose id/name/arguments each arrive split across frames and
    # split byte-at-a-time (mid-`arguments`, mid-frame, CRLF). Adapter must fold them by
    # index and emit exactly two ToolInput with parsed-dict args after [DONE].
    blob = (
        _data_frame({"role": "assistant"})
        + _tc_frame({"index": 0, "id": "call_a", "function": {"name": "get_", "arguments": ""}})
        + _tc_frame({"index": 1, "id": "call_b", "function": {"name": "set_", "arguments": ""}})
        + _tc_frame({"index": 0, "function": {"name": "time", "arguments": '{"tz":'}})
        + _tc_frame({"index": 1, "function": {"name": "temp", "arguments": '{"v":'}})
        + _tc_frame({"index": 0, "function": {"arguments": '"utc"}'}})
        + _tc_frame({"index": 1, "function": {"arguments": "21}"}})
        + b"data: [DONE]\r\n\r\n"
    )
    deltas = await _collect(hass, mock_chat_log, chunk_bytes(blob, 1), _ctx())

    assert deltas[0] == {"role": "assistant"}  # role-first before the tool_calls delta
    assert len(deltas) == 2
    calls = deltas[1].get("tool_calls")
    assert calls is not None
    assert [(c.id, c.tool_name, c.tool_args) for c in calls] == [
        ("call_a", "get_time", {"tz": "utc"}),
        ("call_b", "set_temp", {"v": 21}),
    ]


async def test_tool_call_with_text_then_call(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # A turn that streams text AND a tool call: content deltas pass through live, the
    # tool_calls delta is flushed once at the end (single assistant block, role-first once).
    blob = (
        _data_frame({"content": "Let me check. "})
        + _tc_frame({"index": 0, "id": "c1", "function": {"name": "get_time", "arguments": "{}"}})
        + b"data: [DONE]\n\n"
    )
    deltas = await _collect(hass, mock_chat_log, [blob], _ctx())
    assert deltas[0] == {"role": "assistant"}
    assert deltas[1] == {"content": "Let me check. "}
    calls = deltas[2].get("tool_calls")
    assert calls is not None
    assert calls[0].id == "c1"
    assert calls[0].tool_args == {}


async def test_empty_arguments_default_to_empty_object(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # A no-arg call streams no `arguments`; the flush parses "" as {} (never raises).
    blob = _tc_frame({"index": 0, "id": "c1", "function": {"name": "ping"}}) + b"data: [DONE]\n\n"
    deltas = await _collect(hass, mock_chat_log, [blob], _ctx())
    calls = deltas[-1].get("tool_calls")
    assert calls is not None
    assert calls[0].tool_args == {}


async def test_malformed_tool_arguments_skip_only_that_call(
    hass: HomeAssistant, mock_chat_log: MockChatLog, caplog: pytest.LogCaptureFixture
) -> None:
    # Unparsable streamed arguments drop that one call (logged), not the whole turn.
    blob = (
        _tc_frame({"index": 0, "id": "bad", "function": {"name": "a", "arguments": '{"x": '}})
        + _tc_frame({"index": 1, "id": "ok", "function": {"name": "b", "arguments": '{"y": 1}'}})
        + b"data: [DONE]\n\n"
    )
    with caplog.at_level("WARNING"):
        deltas = await _collect(hass, mock_chat_log, [blob], _ctx())

    calls = deltas[-1].get("tool_calls")
    assert calls is not None
    assert [(call.id, call.tool_args) for call in calls] == [("ok", {"y": 1})]
    assert "bad" in caplog.text


async def test_only_malformed_tool_call_keeps_the_text_reply(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # With every call dropped no tool_calls delta is emitted; streamed text still stands.
    blob = (
        _data_frame({"content": "On it."})
        + _tc_frame({"index": 0, "id": "bad", "function": {"name": "a", "arguments": "not json"}})
        + b"data: [DONE]\n\n"
    )
    deltas = await _collect(hass, mock_chat_log, [blob], _ctx())

    assert deltas == [{"role": "assistant"}, {"content": "On it."}]


async def test_tools_field_sent_when_llm_api_set(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    mock_chat_log.llm_api = cast(llm.APIInstance, _FakeLLMApi([_FakeTool()]))
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = _adapter(hass, session)
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx())]
    tools = _posted_body(session)["tools"]
    assert len(tools) == 1
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "get_time"
    assert tools[0]["function"]["description"] == "Return the current time"
    assert isinstance(tools[0]["function"]["parameters"], dict)


async def _posted_tool_parameters(hass: HomeAssistant, chat_log: MockChatLog, parameters: Any) -> Any:
    """Send one turn offering a single tool with ``parameters``; return its rendered JSON Schema."""
    tool = _FakeTool()
    tool.parameters = parameters
    # selector_serializer is what the real Assist API instance passes as custom_serializer.
    chat_log.llm_api = cast(llm.APIInstance, _FakeLLMApi([tool], custom_serializer=llm.selector_serializer))
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    _ = [d async for d in _adapter(hass, session).stream_turn(chat_log, _input(), _ctx())]
    return _posted_body(session)["tools"][0]["function"]["parameters"]


async def test_tool_parameters_render_voluptuous_schema(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    parameters = await _posted_tool_parameters(hass, mock_chat_log, vol_tool_schema())
    assert parameters["properties"] == TOOL_SCHEMA_PROPERTIES
    assert parameters["required"] == ["name"]


async def test_tool_parameters_render_probatio_schema(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # HA >= 2026.9 tool schemas are probatio; voluptuous_openapi renders them as "UNSUPPORTED".
    parameters = await _posted_tool_parameters(hass, mock_chat_log, probatio_tool_schema())
    assert parameters["properties"] == TOOL_SCHEMA_PROPERTIES
    assert parameters["required"] == ["name"]


async def test_tools_field_absent_without_llm_api(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = _adapter(hass, session)
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx())]
    assert "tools" not in _posted_body(session)


async def test_history_replays_tool_calls_and_results_default_str(
    hass: HomeAssistant, mock_chat_log: MockChatLog
) -> None:
    # A prior tool turn is replayed: the assistant message carries `tool_calls`, and the
    # tool result — holding a non-JSON-native datetime — serializes via default=str.
    now = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    content: list[conversation.Content] = [
        conversation.SystemContent(content="sys"),
        conversation.UserContent(content="what time is it"),
        conversation.AssistantContent(
            agent_id="a",
            content=None,
            tool_calls=[llm.ToolInput(id="call_1", tool_name="get_time", tool_args={"tz": "utc"})],
        ),
        tool_result_content(agent_id="a", tool_call_id="call_1", tool_name="get_time", data={"now": now}),
    ]
    mock_chat_log.content = content
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    adapter = _adapter(hass, session)
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx())]
    messages = _posted_body(session)["messages"]

    assert messages[2] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "get_time", "arguments": '{"tz": "utc"}'}}
        ],
    }
    assert messages[3]["role"] == "tool"
    assert messages[3]["tool_call_id"] == "call_1"
    assert json.loads(messages[3]["content"]) == {"data": {"now": str(now)}, "error": False}


@pytest.mark.skipif(not HA_HAS_TOOL_RESULT_OBJ, reason="llm.ToolResult.error needs HA >= 2026.10")
async def test_failed_tool_result_is_flagged_as_error(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # A failed tool call reaches the model flagged, as core openai/ollama send it.
    mock_chat_log.content = [
        conversation.SystemContent(content="sys"),
        tool_result_content(agent_id="a", tool_call_id="c1", tool_name="t", data={"error": "boom"}, error=True),
    ]
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    _ = [d async for d in _adapter(hass, session).stream_turn(mock_chat_log, _input(), _ctx())]

    assert json.loads(_posted_body(session)["messages"][1]["content"]) == {"data": {"error": "boom"}, "error": True}


class _UndescribedTool(_FakeTool):
    description = None


async def test_tool_without_description_omits_field(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # A tool with no description sends no "description" key rather than null.
    mock_chat_log.llm_api = cast(llm.APIInstance, _FakeLLMApi([_UndescribedTool()]))
    session = fake_aiohttp_session(response=FakeStreamResponse([b"data: [DONE]\n\n"]))
    _ = [d async for d in _adapter(hass, session).stream_turn(mock_chat_log, _input(), _ctx())]

    assert "description" not in _posted_body(session)["tools"][0]["function"]


# --- registration ---------------------------------------------------------------


def test_registered_in_factory() -> None:
    assert BACKEND_TO_CLS["openai_compat"] is OpenAICompatAdapter
    assert get_backend_cls("openai_compat") is OpenAICompatAdapter
    assert OpenAICompatAdapter.backend_type == "openai_compat"
    assert OpenAICompatAdapter.supports_ha_tools is True


# --- connection probe / model list ----------------------------------------------


class _FakeGetResponse:
    """Minimal ``GET`` response stand-in with ``status`` + async ``json()``."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def json(self) -> Any:
        return self._payload

    async def __aenter__(self) -> _FakeGetResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _get_session(*, response: _FakeGetResponse | None = None, exc: Exception | None = None) -> MagicMock:
    session = MagicMock()
    if exc is not None:
        session.get = MagicMock(side_effect=exc)
    else:
        session.get = MagicMock(return_value=response)
    return session


def _patch_session(session: MagicMock) -> Any:
    return patch(
        "custom_components.llm_middleman.backends.openai_compat.async_get_clientsession",
        return_value=session,
    )


async def test_validate_connection_ok(hass: HomeAssistant) -> None:
    session = _get_session(response=_FakeGetResponse(200, {"data": []}))
    with _patch_session(session):
        assert await OpenAICompatAdapter.async_validate_connection(hass, _CONNECTION) is None
    assert session.get.call_args.args[0] == f"{TEST_BASE_URL}/v1/models"


async def test_validate_connection_auth_error(hass: HomeAssistant) -> None:
    session = _get_session(response=_FakeGetResponse(401, {}))
    with _patch_session(session), pytest.raises(BackendAuthError):
        await OpenAICompatAdapter.async_validate_connection(hass, _CONNECTION)


async def test_validate_connection_server_error(hass: HomeAssistant) -> None:
    session = _get_session(response=_FakeGetResponse(500, {}))
    with _patch_session(session), pytest.raises(BackendConnectionError):
        await OpenAICompatAdapter.async_validate_connection(hass, _CONNECTION)


async def test_validate_connection_transport_error(hass: HomeAssistant) -> None:
    session = _get_session(exc=aiohttp.ClientError())
    with _patch_session(session), pytest.raises(BackendConnectionError):
        await OpenAICompatAdapter.async_validate_connection(hass, _CONNECTION)


async def test_validate_connection_non_object_body_is_connection_error(hass: HomeAssistant) -> None:
    """A 200 whose JSON body is not an object fails validation instead of passing silently."""
    session = _get_session(response=_FakeGetResponse(200, [{"id": "gpt-4"}]))
    with _patch_session(session), pytest.raises(BackendConnectionError):
        await OpenAICompatAdapter.async_validate_connection(hass, _CONNECTION)


async def test_list_models_parses_ids(hass: HomeAssistant) -> None:
    payload = {"data": [{"id": "gpt-4"}, {"id": "llama-3"}, {"object": "no-id"}]}
    session = _get_session(response=_FakeGetResponse(200, payload))
    with _patch_session(session):
        models = await OpenAICompatAdapter.async_list_models(hass, _CONNECTION)
    assert models == ["gpt-4", "llama-3"]


async def test_real_session_type_contract(hass: HomeAssistant) -> None:
    # The constructor stores the shared session unchanged (parity with base tests).
    session = async_get_clientsession(hass)
    adapter = OpenAICompatAdapter(hass, session, _CONNECTION)
    assert adapter.session is session


async def test_streaming_post_honors_agent_timeout(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # The per-agent CONF_TIMEOUT plus the shared idle deadline reach the wire call.
    response = FakeStreamResponse([b"data: [DONE]\n\n"])
    session = fake_aiohttp_session(response=response)
    adapter = OpenAICompatAdapter(hass, session, _CONNECTION)
    _ = [d async for d in adapter.stream_turn(mock_chat_log, _input(), _ctx(timeout=120))]
    timeout = session.post.call_args.kwargs["timeout"]
    assert timeout.total is None
    assert timeout.sock_connect == 120
    assert timeout.sock_read == IDLE_TIMEOUT
