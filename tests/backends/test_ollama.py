"""Tests for the Ollama-native adapter (LLMM-010).

Drives **raw bytes** through the real NDJSON parser (chunk boundaries split
mid-JSON-object and mid-line via the conftest harness), never pre-split lines.
Covers the parser edge cases (done-with-no-delta, silent EOF, error-after-deltas,
malformed JSON, no-trailing-newline, whitespace), stateless trim, option gating,
and the ``/api/tags`` probe.
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

from custom_components.llm_middleman.backends import BACKEND_TO_CLS, get_backend_cls
from custom_components.llm_middleman.backends._history import trim_history
from custom_components.llm_middleman.backends.base import (
    BackendAuthError,
    BackendConnectionError,
    BackendStreamError,
    TurnContext,
)
from custom_components.llm_middleman.backends.ollama import OllamaAdapter, _convert_content
from custom_components.llm_middleman.const import (
    BACKEND_OLLAMA,
    CONF_BASE_URL,
    CONF_KEEP_ALIVE,
    CONF_MAX_HISTORY,
    CONF_MODEL,
    CONF_NUM_CTX,
    CONF_THINK,
    CONF_TIMEOUT,
    IDLE_TIMEOUT,
)
from tests.conftest import (
    TEST_BASE_URL,
    TOOL_SCHEMA_PROPERTIES,
    FakeStreamResponse,
    MockChatLog,
    chunk_bytes,
    external_tool_history,
    fake_aiohttp_session,
    probatio_tool_schema,
    tool_result_content,
    vol_tool_schema,
)

Delta = conversation.AssistantContentDeltaDict


def _make_input() -> conversation.ConversationInput:
    return conversation.ConversationInput(
        text="hi",
        context=Context(),
        conversation_id="conv-1",
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="agent-1",
    )


def _adapter(hass: HomeAssistant, session: MagicMock) -> OllamaAdapter:
    return OllamaAdapter(hass, session, {CONF_BASE_URL: TEST_BASE_URL})


async def _run(
    adapter: OllamaAdapter,
    chat_log: conversation.ChatLog,
    *,
    options: dict[str, Any] | None = None,
) -> list[Delta]:
    ctx = TurnContext(options=options or {CONF_MODEL: "llama3"}, memory_key="k")
    return [delta async for delta in adapter.stream_turn(chat_log, _make_input(), ctx)]


# --- NDJSON parser edge cases (raw bytes through the real parser) ---

_HAPPY = (
    b'{"message":{"content":"Hel"},"done":false}\n'
    b'{"message":{"content":"lo"},"done":false}\n'
    b'{"message":{"content":""},"done":true}\n'
)


@pytest.mark.parametrize(
    "chunks",
    [
        chunk_bytes(_HAPPY, 1),  # byte-at-a-time: splits every line and object
        chunk_bytes(_HAPPY, [10, 25, 55, 90]),  # explicit mid-object / mid-line cuts
        [_HAPPY],  # single chunk
    ],
    ids=["byte-at-a-time", "mid-object-cuts", "single-chunk"],
)
async def test_happy_path_split_chunks(
    hass: HomeAssistant, mock_chat_log: conversation.ChatLog, chunks: list[bytes]
) -> None:
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse(chunks)))
    deltas = await _run(adapter, mock_chat_log)
    assert deltas == [{"role": "assistant"}, {"content": "Hel"}, {"content": "lo"}]


async def test_no_trailing_newline(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    blob = b'{"message":{"content":"Hi"},"done":false}\n{"message":{"content":"!"},"done":true}'
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse(chunk_bytes(blob, 1))))
    deltas = await _run(adapter, mock_chat_log)
    assert deltas == [{"role": "assistant"}, {"content": "Hi"}, {"content": "!"}]


async def test_eof_without_done(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    # Stream ends after a content object with no done:true -> generator terminates
    # cleanly; LLMM-005's entity guard supplies the final AssistantContent.
    blob = b'{"message":{"content":"partial"},"done":false}\n'
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse(chunk_bytes(blob, 3))))
    deltas = await _run(adapter, mock_chat_log)
    assert deltas == [{"role": "assistant"}, {"content": "partial"}]


async def test_whitespace_preserved(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    blob = b'{"message":{"content":"Hello"},"done":false}\n{"message":{"content":" world"},"done":true}\n'
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse(chunk_bytes(blob, 1))))
    deltas = await _run(adapter, mock_chat_log)
    assert deltas == [{"role": "assistant"}, {"content": "Hello"}, {"content": " world"}]


async def test_done_with_no_delta(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    # Terminal object carries empty content -> no role, no content deltas at all.
    blob = b'{"message":{"content":""},"done":true}\n'
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse([blob])))
    deltas = await _run(adapter, mock_chat_log)
    assert deltas == []


async def test_thinking_emitted_role_first(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    blob = b'{"message":{"thinking":"hmm","content":""},"done":false}\n{"message":{"content":"ok"},"done":true}\n'
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse(chunk_bytes(blob, 1))))
    deltas = await _run(adapter, mock_chat_log)
    assert deltas == [{"role": "assistant"}, {"thinking_content": "hmm"}, {"content": "ok"}]


async def test_malformed_json_raises(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    blob = b'{"message":{"content":"Hi"},"done":false}\nnot valid json\n'
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse(chunk_bytes(blob, 1))))
    with pytest.raises(BackendStreamError, match="Malformed NDJSON"):
        await _run(adapter, mock_chat_log)


async def test_error_after_deltas_propagates(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    # Transport raises mid-stream after >=1 delta has been emitted.
    chunks = [
        b'{"message":{"content":"Hi"},"done":false}\n',
        b'{"message":{"content":"more"},"done":false}\n',
    ]
    boom = aiohttp.ClientPayloadError("stream broke")
    response = FakeStreamResponse(chunks, raise_after=1, exc=boom)
    adapter = _adapter(hass, fake_aiohttp_session(response=response))

    seen: list[Delta] = []
    with pytest.raises(aiohttp.ClientPayloadError):
        async for delta in adapter.stream_turn(mock_chat_log, _make_input(), TurnContext(options={}, memory_key="k")):
            seen.append(delta)
    assert seen == [{"role": "assistant"}, {"content": "Hi"}]


async def test_oversize_ndjson_line_skipped_stream_continues(
    hass: HomeAssistant, mock_chat_log: conversation.ChatLog
) -> None:
    """An over-cap NDJSON line is dropped (bounded memory); later lines still stream."""
    huge = b'{"message":{"content":"' + b"x" * 70000 + b'"}}\n'
    blob = huge + b'{"message":{"content":"ok"}}\n{"message":{"content":""},"done":true}\n'
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse(chunk_bytes(blob, 4096))))
    deltas = await _run(adapter, mock_chat_log)
    assert deltas == [{"role": "assistant"}, {"content": "ok"}]


async def test_non_200_status_raises(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    response = FakeStreamResponse([], status=500)
    adapter = _adapter(hass, fake_aiohttp_session(response=response))
    with pytest.raises(BackendConnectionError, match="HTTP 500"):
        await _run(adapter, mock_chat_log)


# --- Request assembly: stateless trim + option gating ---


async def test_trim_history_in_request_body(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    mock_chat_log.content.clear()
    mock_chat_log.content.extend(
        [
            conversation.SystemContent(content="sys"),
            conversation.UserContent(content="u1"),
            conversation.AssistantContent(agent_id="a", content="a1"),
            conversation.UserContent(content="u2"),
            conversation.AssistantContent(agent_id="a", content="a2"),
            conversation.UserContent(content="u3"),
        ]
    )
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    adapter = _adapter(hass, session)
    await _run(adapter, mock_chat_log, options={CONF_MODEL: "llama3", CONF_MAX_HISTORY: 1})

    body = session.post.call_args.kwargs["json"]
    # system + last 2*1+1 = system + [u2, a2, u3]
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]
    assert body["messages"][-1]["content"] == "u3"


async def test_options_included_only_when_set(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    adapter = _adapter(hass, session)
    await _run(
        adapter,
        mock_chat_log,
        options={CONF_MODEL: "llama3", CONF_NUM_CTX: 4096, CONF_KEEP_ALIVE: 300, CONF_THINK: True},
    )
    body = session.post.call_args.kwargs["json"]
    assert body["model"] == "llama3"
    assert body["stream"] is True
    assert body["options"] == {"num_ctx": 4096}
    assert body["keep_alive"] == "300s"
    assert body["think"] is True


async def test_options_absent_when_unset(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    adapter = _adapter(hass, session)
    await _run(adapter, mock_chat_log, options={CONF_MODEL: "llama3"})
    body = session.post.call_args.kwargs["json"]
    assert "options" not in body
    assert "keep_alive" not in body
    assert "think" not in body


async def test_keep_alive_forever_sentinel(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    adapter = _adapter(hass, session)
    await _run(adapter, mock_chat_log, options={CONF_MODEL: "llama3", CONF_KEEP_ALIVE: -1})
    body = session.post.call_args.kwargs["json"]
    assert body["keep_alive"] == -1  # literal int, not "-1s"


async def test_base_url_trailing_slash_stripped(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    adapter = OllamaAdapter(hass, session, {CONF_BASE_URL: "http://host:11434/"})
    await _run(adapter, mock_chat_log)
    assert session.post.call_args.args[0] == "http://host:11434/api/chat"


# --- Auth header ---


async def test_auth_header_sent_when_api_key_set(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    adapter = OllamaAdapter(hass, session, {CONF_BASE_URL: TEST_BASE_URL, "api_key": "sk-123"})
    await _run(adapter, mock_chat_log)
    assert session.post.call_args.kwargs["headers"] == {"Authorization": "Bearer sk-123"}


# --- /api/tags probe ---


class _FakeGetResponse:
    def __init__(self, *, status: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.status = status
        self._payload = payload if payload is not None else {}

    async def json(self) -> dict[str, Any]:
        return self._payload

    async def __aenter__(self) -> _FakeGetResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _fake_get_session(*, response: _FakeGetResponse | None = None, exc: Exception | None = None) -> MagicMock:
    session = MagicMock()
    if exc is not None:
        session.get = MagicMock(side_effect=exc)
    else:
        session.get = MagicMock(return_value=response)
    return session


async def test_validate_connection_ok(hass: HomeAssistant) -> None:
    session = _fake_get_session(response=_FakeGetResponse(payload={"models": []}))
    with patch(
        "custom_components.llm_middleman.backends.ollama.async_get_clientsession",
        return_value=session,
    ):
        assert await OllamaAdapter.async_validate_connection(hass, {CONF_BASE_URL: TEST_BASE_URL}) is None
    assert session.get.call_args.args[0] == f"{TEST_BASE_URL}/api/tags"


async def test_validate_connection_error(hass: HomeAssistant) -> None:
    session = _fake_get_session(exc=aiohttp.ClientConnectionError("refused"))
    with (
        patch(
            "custom_components.llm_middleman.backends.ollama.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(BackendConnectionError),
    ):
        await OllamaAdapter.async_validate_connection(hass, {CONF_BASE_URL: TEST_BASE_URL})


async def test_validate_connection_auth_error(hass: HomeAssistant) -> None:
    session = _fake_get_session(response=_FakeGetResponse(status=401))
    with (
        patch(
            "custom_components.llm_middleman.backends.ollama.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(BackendAuthError),
    ):
        await OllamaAdapter.async_validate_connection(hass, {CONF_BASE_URL: TEST_BASE_URL})


async def test_list_models(hass: HomeAssistant) -> None:
    payload = {"models": [{"model": "llama3:latest"}, {"model": "qwen3:4b"}]}
    session = _fake_get_session(response=_FakeGetResponse(payload=payload))
    with patch(
        "custom_components.llm_middleman.backends.ollama.async_get_clientsession",
        return_value=session,
    ):
        models = await OllamaAdapter.async_list_models(hass, {CONF_BASE_URL: TEST_BASE_URL})
    assert models == ["llama3:latest", "qwen3:4b"]


# --- tools (LLMM-015) ---


class _FakeTool:
    """Minimal duck-typed ``llm.Tool`` (name/description/voluptuous parameters)."""

    name = "get_time"
    description = "Return the current time"
    parameters = vol.Schema({})


class _FakeLLMApi:
    """Duck-typed ``llm.APIInstance`` exposing only what the adapter reads."""

    def __init__(self, tools: list[Any], custom_serializer: Callable[[Any], Any] | None = None) -> None:
        self.tools = tools
        self.custom_serializer = custom_serializer


async def test_native_tool_call_extraction(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    # A message chunk carries a whole tool_calls object (args already a dict), a later
    # chunk closes with done:true -> exactly one ToolInput, role-first before it.
    blob = (
        b'{"message":{"role":"assistant","content":"",'
        b'"tool_calls":[{"function":{"name":"get_time","arguments":{"tz":"utc"}}}]}}\n'
        b'{"message":{"content":""},"done":true}\n'
    )
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse(chunk_bytes(blob, 1))))
    deltas = await _run(adapter, mock_chat_log)
    assert deltas[0] == {"role": "assistant"}  # role-first before the tool_calls delta
    calls = deltas[1].get("tool_calls")
    assert calls is not None
    assert len(calls) == 1
    assert calls[0].tool_name == "get_time"
    assert calls[0].tool_args == {"tz": "utc"}


async def test_malformed_tool_args_repaired(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    # Empty/None values are dropped (they fail HA intent parsing); a stringified JSON
    # list is parsed back — the small-model repair from core ollama's _parse_tool_args.
    args = '{"area": "", "name": null, "domain": "light", "extra": "[1, 2]"}'
    blob = (
        b'{"message":{"role":"assistant","content":"",'
        b'"tool_calls":[{"function":{"name":"set_light","arguments":' + args.encode() + b"}}]}}\n"
        b'{"message":{"content":""},"done":true}\n'
    )
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse(chunk_bytes(blob, 7))))
    deltas = await _run(adapter, mock_chat_log)
    calls = deltas[-1].get("tool_calls")
    assert calls is not None
    assert calls[0].tool_args == {"domain": "light", "extra": [1, 2]}


async def test_tools_field_sent_when_llm_api_set(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    mock_chat_log.llm_api = cast(llm.APIInstance, _FakeLLMApi([_FakeTool()]))
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    adapter = _adapter(hass, session)
    await _run(adapter, mock_chat_log)
    tools = session.post.call_args.kwargs["json"]["tools"]
    assert len(tools) == 1
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "get_time"
    assert tools[0]["function"]["description"] == "Return the current time"
    assert isinstance(tools[0]["function"]["parameters"], dict)


class _UndescribedTool(_FakeTool):
    description = None


async def test_tool_without_description_omits_field(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    """Ollama's wire shape drops ``description`` when the tool has none (core ollama)."""
    mock_chat_log.llm_api = cast(llm.APIInstance, _FakeLLMApi([_UndescribedTool()]))
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    await _run(_adapter(hass, session), mock_chat_log)
    assert "description" not in session.post.call_args.kwargs["json"]["tools"][0]["function"]


async def _posted_tool_parameters(hass: HomeAssistant, chat_log: conversation.ChatLog, parameters: Any) -> Any:
    """Send one turn offering a single tool with ``parameters``; return its rendered JSON Schema."""
    tool = _FakeTool()
    tool.parameters = parameters
    # selector_serializer is what the real Assist API instance passes as custom_serializer.
    chat_log.llm_api = cast(llm.APIInstance, _FakeLLMApi([tool], custom_serializer=llm.selector_serializer))
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    await _run(_adapter(hass, session), chat_log)
    return session.post.call_args.kwargs["json"]["tools"][0]["function"]["parameters"]


async def test_tool_parameters_render_voluptuous_schema(
    hass: HomeAssistant, mock_chat_log: conversation.ChatLog
) -> None:
    parameters = await _posted_tool_parameters(hass, mock_chat_log, vol_tool_schema())
    assert parameters["properties"] == TOOL_SCHEMA_PROPERTIES
    assert parameters["required"] == ["name"]


async def test_tool_parameters_render_probatio_schema(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    # HA >= 2026.9 tool schemas are probatio; voluptuous_openapi renders them as "UNSUPPORTED".
    parameters = await _posted_tool_parameters(hass, mock_chat_log, probatio_tool_schema())
    assert parameters["properties"] == TOOL_SCHEMA_PROPERTIES
    assert parameters["required"] == ["name"]


async def test_tools_field_absent_without_llm_api(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    adapter = _adapter(hass, session)
    await _run(adapter, mock_chat_log)
    assert "tools" not in session.post.call_args.kwargs["json"]


async def test_history_replays_tool_calls_and_results_default_str(
    hass: HomeAssistant, mock_chat_log: conversation.ChatLog
) -> None:
    # A prior tool turn is replayed: the assistant message carries `tool_calls` in
    # Ollama's {"function": {name, arguments}} shape, and the tool result — holding a
    # non-JSON-native datetime — serializes via default=str.
    now = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    mock_chat_log.content.clear()
    mock_chat_log.content.extend(
        [
            conversation.SystemContent(content="sys"),
            conversation.UserContent(content="what time is it"),
            conversation.AssistantContent(
                agent_id="a",
                content=None,
                tool_calls=[llm.ToolInput(id="call_1", tool_name="get_time", tool_args={"tz": "utc"})],
            ),
            tool_result_content(agent_id="a", tool_call_id="call_1", tool_name="get_time", data={"now": now}),
        ]
    )
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    adapter = _adapter(hass, session)
    await _run(adapter, mock_chat_log)
    messages = session.post.call_args.kwargs["json"]["messages"]

    assert messages[2] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "get_time", "arguments": {"tz": "utc"}}}],
    }
    assert messages[3]["role"] == "tool"
    assert json.loads(messages[3]["content"]) == {"data": {"now": str(now)}, "error": False}


async def test_history_skips_external_tool_calls_and_results(
    hass: HomeAssistant, mock_chat_log: conversation.ChatLog
) -> None:
    # Backend-run (external) calls from another agent must not be replayed as if this
    # model made them; the HA-run call and its result still are.
    mock_chat_log.content.clear()
    mock_chat_log.content.extend(external_tool_history())
    session = fake_aiohttp_session(response=FakeStreamResponse([b'{"message":{"content":""},"done":true}\n']))
    await _run(_adapter(hass, session), mock_chat_log)
    messages = session.post.call_args.kwargs["json"]["messages"]

    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user", "assistant", "tool"]
    assert messages[2] == {"role": "assistant", "content": "It is sunny."}
    assert [call["function"]["name"] for call in messages[4]["tool_calls"]] == ["HassTurnOn"]
    assert json.loads(messages[5]["content"]) == {"data": {"ok": True}, "error": False}


# --- Registration + classvars ---


def test_registration_and_classvars() -> None:
    assert BACKEND_OLLAMA == "ollama"
    assert OllamaAdapter.backend_type == BACKEND_OLLAMA
    assert OllamaAdapter.supports_ha_tools is True
    assert BACKEND_TO_CLS[BACKEND_OLLAMA] is OllamaAdapter
    assert get_backend_cls(BACKEND_OLLAMA) is OllamaAdapter


# --- Shared trim helper unit coverage ---


def test_trim_history_keeps_all_below_threshold() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    assert trim_history(messages, 5) == messages  # only 1 previous round < 5


def test_trim_history_zero_keeps_all() -> None:
    messages = [{"role": "user", "content": f"u{i}"} for i in range(10)]
    assert trim_history(messages, 0) == messages


def test_convert_content_maps_roles() -> None:
    assert _convert_content(conversation.SystemContent(content="s")) == {"role": "system", "content": "s"}
    assert _convert_content(conversation.UserContent(content="u")) == {"role": "user", "content": "u"}
    asst = conversation.AssistantContent(agent_id="a", content="hi", thinking_content="why")
    assert _convert_content(asst) == {"role": "assistant", "content": "hi", "thinking": "why"}


async def test_streaming_post_honors_agent_timeout(hass: HomeAssistant, mock_chat_log: MockChatLog) -> None:
    # The per-agent CONF_TIMEOUT plus the shared idle deadline reach the wire call.
    response = FakeStreamResponse([b'{"message":{"role":"assistant","content":"x"},"done":true}\n'])
    session = fake_aiohttp_session(response=response)
    adapter = _adapter(hass, session)
    await _run(adapter, mock_chat_log, options={CONF_MODEL: "llama3", CONF_TIMEOUT: 45})
    timeout = session.post.call_args.kwargs["timeout"]
    assert timeout.total is None
    assert timeout.sock_connect == 45
    assert timeout.sock_read == IDLE_TIMEOUT


# --- token stats (trace) ----------------------------------------------------------


async def test_done_frame_counts_traced_as_stats(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    blob = (
        b'{"message":{"content":"Hi"},"done":false}\n'
        b'{"message":{"content":""},"done":true,"prompt_eval_count":42,"eval_count":9}\n'
    )
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse(chunk_bytes(blob, 4))))
    with patch.object(mock_chat_log, "async_trace") as trace:
        deltas = await _run(adapter, mock_chat_log)
    assert deltas == [{"role": "assistant"}, {"content": "Hi"}]
    trace.assert_called_once_with({"stats": {"input_tokens": 42, "output_tokens": 9}})


async def test_done_frame_missing_prompt_count_defaults_zero(
    hass: HomeAssistant, mock_chat_log: conversation.ChatLog
) -> None:
    # Ollama omits prompt_eval_count when the whole prompt was served from its cache.
    blob = b'{"message":{"content":"Hi"},"done":true,"eval_count":3}\n'
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse([blob])))
    with patch.object(mock_chat_log, "async_trace") as trace:
        await _run(adapter, mock_chat_log)
    trace.assert_called_once_with({"stats": {"input_tokens": 0, "output_tokens": 3}})


async def test_no_counts_no_trace(hass: HomeAssistant, mock_chat_log: conversation.ChatLog) -> None:
    adapter = _adapter(hass, fake_aiohttp_session(response=FakeStreamResponse([_HAPPY])))
    with patch.object(mock_chat_log, "async_trace") as trace:
        await _run(adapter, mock_chat_log)
    trace.assert_not_called()
