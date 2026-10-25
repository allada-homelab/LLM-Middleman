"""HA tool formatting shared by the tool-capable presets (openai-compat, ollama).

Both wire formats nest a tool spec as ``{"type": "function", "function": {...}}`` and
carry a tool result as a JSON string; the per-provider message shapes (tool-call ids,
argument encoding, ``thinking``, image parts) stay in each adapter's ``_convert_content``.
Image attachments (AI Task, LLMM-023) are read and base64-encoded here for both.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from custom_components.llm_middleman.const import DOMAIN

from ._compat import schema_to_openapi, tool_result_data

# One image attachment ready for the wire: (MIME type, base64-encoded bytes).
type EncodedImage = tuple[str, str]


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


def replayable_content(content: Iterable[conversation.Content]) -> list[conversation.Content]:
    """Drop backend-run (``external``) tool calls and their results from a chat-log replay.

    External calls are recorded by presets whose backend runs its own tools (LangGraph,
    Dify). A stateless-replay backend never made them, so replaying them after an agent
    switch would send tool calls/results it can't match. An assistant block left with no
    text, thinking, or HA-run calls is dropped entirely.
    """
    external_ids: set[str] = set()
    kept: list[conversation.Content] = []
    for item in content:
        if isinstance(item, conversation.AssistantContent) and item.tool_calls:
            local = [call for call in item.tool_calls if not call.external]
            if len(local) < len(item.tool_calls):
                external_ids.update(call.id for call in item.tool_calls if call.external)
                if not (local or item.content or item.thinking_content):
                    continue
                item = dataclasses.replace(item, tool_calls=local or None)
        elif isinstance(item, conversation.ToolResultContent) and item.tool_call_id in external_ids:
            continue
        kept.append(item)
    return kept


async def async_load_images(hass: HomeAssistant, items: Sequence[conversation.Content]) -> list[list[EncodedImage]]:
    """Return each item's image attachments base64-encoded (``[]`` when it has none).

    Only ``UserContent`` carries attachments (HA's AI Task puts them there). A non-image
    attachment raises a translated error before any file is read; the reads run in the
    executor because attachment files can be large. Attachments are sent whole, uncapped
    (core openai/ollama do the same) — the server's request-size limit is the bound.
    """
    attachments = [list(item.attachments or ()) if isinstance(item, conversation.UserContent) else [] for item in items]
    for group in attachments:
        for attachment in group:
            if not attachment.mime_type.startswith("image/"):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="unsupported_attachment_type",
                    translation_placeholders={"mime_type": attachment.mime_type},
                )
    if not any(attachments):
        return [[] for _ in items]

    def _read() -> list[list[EncodedImage]]:
        return [
            [(a.mime_type, base64.b64encode(a.path.read_bytes()).decode("ascii")) for a in group]
            for group in attachments
        ]

    return await hass.async_add_executor_job(_read)
