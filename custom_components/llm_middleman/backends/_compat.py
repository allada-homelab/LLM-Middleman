"""Version-tolerant shims for HA tool APIs that changed across supported HA releases.

The integration supports HA from the hacs.json floor through current, and HA moved
two tool-loop APIs underneath it:

- HA 2026.9 swapped voluptuous for ``probatio``: ``llm.Tool.parameters`` became a
  ``probatio.Schema``, which ``voluptuous_openapi.convert`` cannot render (it raises
  ``TypeError: unhashable type: 'Schema'``).
- HA 2026.10 replaced ``ToolResultContent.tool_result`` (the JSON payload) with
  ``result: llm.ToolResult`` (payload in ``.data``); the old attribute is a
  deprecated shim that logs for custom integrations.

The external-tool helpers below build on the second change: presets whose backend runs
its own tools (LangGraph, Dify) surface that activity in the chat log without HA
executing anything.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from homeassistant.components import conversation
from homeassistant.helpers import llm
from voluptuous_openapi import convert  # pyright: ignore[reportUnknownVariableType]

type AssistantDelta = conversation.AssistantContentDeltaDict


def schema_to_openapi(schema: Any, custom_serializer: Callable[[Any], Any] | None) -> dict[str, Any]:
    """Render an HA tool parameter schema (probatio or voluptuous) as JSON Schema.

    The probatio branch mirrors core ``ollama/entity.py:_format_tool`` (HA 2026.10).
    """
    try:
        import probatio
    except ImportError:  # HA < 2026.9 does not ship probatio; its tool schemas are voluptuous
        pass
    else:
        if isinstance(schema, probatio.Schema):
            return probatio.to_openapi(schema, custom_serializer=custom_serializer, openapi_version="3.1.0")
    return cast(dict[str, Any], convert(schema, custom_serializer=custom_serializer))


def tool_result_data(content: conversation.ToolResultContent) -> Any:
    """Return a tool result's JSON payload on both the HA >= 2026.10 and older APIs."""
    if hasattr(content, "result"):
        return content.result.data
    return content.tool_result


# HA 2026.10 added ``result: llm.ToolResult`` to the tool-result delta; older HA only
# accepts the bare ``tool_result`` payload (and 2026.10 logs a deprecation for it).
_DELTA_HAS_RESULT_OBJ = "result" in conversation.ToolResultContentDeltaDict.__annotations__


def external_tool_call_delta(tool_call_id: str, tool_name: str, tool_args: Mapping[str, Any]) -> AssistantDelta:
    """Return an assistant delta recording a tool call the *backend* already ran.

    ``external=True`` makes HA log the call in the chat log (and the Assist chat) without
    executing it. The delta opens a new assistant block (it carries ``role``).

    Contract for callers: every external call should be followed by
    :func:`external_tool_result_delta`, and the stream must not END on a tool result —
    the entity's tool loop re-enters while ``chat_log.unresponded_tool_results`` is true,
    which would re-send the turn. Yield a ``{"role": "assistant"}`` block with non-empty
    content after the last result.
    """
    call = llm.ToolInput(tool_name=tool_name, tool_args=dict(tool_args), id=tool_call_id, external=True)
    return {"role": "assistant", "tool_calls": [call]}


def external_tool_result_delta(
    tool_call_id: str, tool_name: str, result: Any, *, error: bool = False
) -> AssistantDelta:
    """Return a tool-result delta for an external call, on both HA tool-result APIs.

    ``result`` that is not a JSON object (a backend tool's plain-text output, a list) is
    wrapped as ``{"result": value}`` because HA types tool results as a JSON object.
    ``error`` is carried on HA >= 2026.10 (``llm.ToolResult.error``) and dropped on older HA,
    which has no error flag.

    The value is a ``ToolResultContentDeltaDict``; it is typed as the adapter stream's
    delta type because ``DeltaStream`` is declared assistant-only, while HA's
    ``async_add_delta_content_stream`` (the only consumer) accepts both.
    """
    data: dict[str, Any] = cast(dict[str, Any], result) if isinstance(result, dict) else {"result": result}
    delta: dict[str, Any] = {"role": "tool_result", "tool_call_id": tool_call_id, "tool_name": tool_name}
    if _DELTA_HAS_RESULT_OBJ:
        delta["result"] = llm.ToolResult(data=data, error=error)
    else:
        delta["tool_result"] = data
    return cast(AssistantDelta, delta)
