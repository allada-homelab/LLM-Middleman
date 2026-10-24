"""Version-tolerant shims for HA tool APIs that changed across supported HA releases.

The integration supports HA from the hacs.json floor through current, and HA moved
two tool-loop APIs underneath it:

- HA 2026.9 swapped voluptuous for ``probatio``: ``llm.Tool.parameters`` became a
  ``probatio.Schema``, which ``voluptuous_openapi.convert`` cannot render (it raises
  ``TypeError: unhashable type: 'Schema'``).
- HA 2026.10 replaced ``ToolResultContent.tool_result`` (the JSON payload) with
  ``result: llm.ToolResult`` (payload in ``.data``); the old attribute is a
  deprecated shim that logs for custom integrations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from homeassistant.components import conversation
from voluptuous_openapi import convert  # pyright: ignore[reportUnknownVariableType]


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
