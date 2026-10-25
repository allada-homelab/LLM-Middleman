"""Tests for the external tool-activity helpers in ``backends/_compat.py`` (LLMM-022).

The deltas are fed through HA's real ``ChatLog.async_add_delta_content_stream`` so the
same assertions hold on the floor HA (``tool_result`` payload) and HA >= 2026.10
(``result: llm.ToolResult``).
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.components import conversation

from custom_components.llm_middleman.backends import _compat
from custom_components.llm_middleman.backends._compat import (
    AssistantDelta,
    external_tool_call_delta,
    external_tool_result_delta,
    schema_to_openapi,
    tool_result_data,
)
from tests.conftest import HA_HAS_TOOL_RESULT_OBJ, MockChatLog


async def _stream(*deltas: AssistantDelta) -> AsyncIterator[AssistantDelta]:
    for delta in deltas:
        yield delta


async def _feed(chat_log: MockChatLog, result: Any, *, error: bool = False) -> list[conversation.Content]:
    """Stream call -> result -> reply into ``chat_log`` and return what was added."""
    deltas = _stream(
        external_tool_call_delta("call_1", "get_weather", {"city": "Paris"}),
        external_tool_result_delta("call_1", "get_weather", result, error=error),
        {"role": "assistant"},
        {"content": "Sunny."},
    )
    return [content async for content in chat_log.async_add_delta_content_stream("conversation.test", deltas)]


async def test_external_call_is_logged_but_not_executed(mock_chat_log: MockChatLog) -> None:
    # No LLM API is configured: a non-external call would raise "No LLM API configured".
    call, _, _ = await _feed(mock_chat_log, {"temp": 21})
    assert isinstance(call, conversation.AssistantContent)
    assert call.tool_calls is not None
    (tool_input,) = call.tool_calls
    assert (tool_input.id, tool_input.tool_name, tool_input.tool_args, tool_input.external) == (
        "call_1",
        "get_weather",
        {"city": "Paris"},
        True,
    )


async def test_external_result_payload_on_this_ha(mock_chat_log: MockChatLog) -> None:
    _, result, _ = await _feed(mock_chat_log, {"temp": 21})
    assert isinstance(result, conversation.ToolResultContent)
    assert (result.tool_call_id, result.tool_name, tool_result_data(result)) == ("call_1", "get_weather", {"temp": 21})


async def test_non_object_result_is_wrapped(mock_chat_log: MockChatLog) -> None:
    _, result, _ = await _feed(mock_chat_log, "sunny and 21C")
    assert isinstance(result, conversation.ToolResultContent)
    assert tool_result_data(result) == {"result": "sunny and 21C"}


async def test_reply_after_result_leaves_no_unresponded_tool_result(mock_chat_log: MockChatLog) -> None:
    await _feed(mock_chat_log, {"temp": 21})
    assert mock_chat_log.unresponded_tool_results is False


@pytest.mark.skipif(not HA_HAS_TOOL_RESULT_OBJ, reason="HA < 2026.10 tool results carry no error flag")
async def test_error_flag_is_carried(mock_chat_log: MockChatLog) -> None:
    _, result, _ = await _feed(mock_chat_log, {"error": "boom"}, error=True)
    assert isinstance(result, conversation.ToolResultContent)
    assert result.result.error is True


# The tests below force the older-HA branches on the locked HA; the floor leg runs them for real.

_DICT_SCHEMA = {"city": str}
_DICT_SCHEMA_OPENAPI: dict[str, Any] = {"type": "object", "properties": {"city": {"type": "string"}}, "required": []}


def test_schema_without_probatio_renders_via_voluptuous_openapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "probatio", None)  # HA < 2026.9: `import probatio` fails
    assert schema_to_openapi(_DICT_SCHEMA, None) == _DICT_SCHEMA_OPENAPI


def test_non_probatio_schema_renders_via_voluptuous_openapi() -> None:
    assert schema_to_openapi(_DICT_SCHEMA, None) == _DICT_SCHEMA_OPENAPI


def test_tool_result_data_reads_pre_2026_10_tool_result() -> None:
    legacy = cast(conversation.ToolResultContent, SimpleNamespace(tool_result={"temp": 21}))
    assert tool_result_data(legacy) == {"temp": 21}


def test_result_delta_carries_bare_payload_before_2026_10(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_compat, "_DELTA_HAS_RESULT_OBJ", False)
    delta = cast(dict[str, Any], external_tool_result_delta("call_1", "get_weather", "sunny"))
    assert delta == {
        "role": "tool_result",
        "tool_call_id": "call_1",
        "tool_name": "get_weather",
        "tool_result": {"result": "sunny"},
    }
