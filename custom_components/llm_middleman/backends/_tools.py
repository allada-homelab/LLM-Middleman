"""HA tool formatting shared by the tool-capable presets (openai-compat, ollama).

Both wire formats nest a tool spec as ``{"type": "function", "function": {...}}`` and
carry a tool result as a JSON string; the per-provider message shapes (tool-call ids,
argument encoding, ``thinking``) stay in each adapter's ``_convert_content``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from homeassistant.components import conversation
from homeassistant.helpers import llm

from ._compat import schema_to_openapi, tool_result_data


def format_function_tool(
    tool: llm.Tool,
    custom_serializer: Callable[[Any], Any] | None,
    *,
    omit_empty_description: bool = False,
) -> dict[str, Any]:
    """Format one HA tool as a ``function`` tool spec.

    ``schema_to_openapi`` renders the probatio/voluptuous schema to JSON Schema;
    ``custom_serializer`` handles HA selector types. Ollama omits ``description`` when the
    tool has none (core ``ollama/entity.py:_format_tool``) -> ``omit_empty_description``;
    OpenAI chat/completions sends it unless it is ``None`` (never a JSON ``null``). The
    unsupported-JSON-Schema-key strip core openai applies is intentionally omitted: most
    OpenAI-compatible servers tolerate
    ``enum``/``anyOf``/… and stripping them silently degrades the schema.
    """
    function: dict[str, Any] = {
        "name": tool.name,
        "parameters": schema_to_openapi(tool.parameters, custom_serializer),
    }
    if tool.description or (tool.description is not None and not omit_empty_description):
        function["description"] = tool.description
    return {"type": "function", "function": function}


def tool_result_error(content: conversation.ToolResultContent) -> bool:
    """Return whether the tool call failed; HA < 2026.10 results carry no error flag."""
    if hasattr(content, "result"):
        return content.result.error
    return False


def tool_result_json(content: conversation.ToolResultContent) -> str:
    """Serialize a tool result as core openai/ollama do: ``{"data": …, "error": bool}``.

    ``default=str`` because time/datetime payloads aren't JSON-native.
    """
    return json.dumps({"data": tool_result_data(content), "error": tool_result_error(content)}, default=str)
