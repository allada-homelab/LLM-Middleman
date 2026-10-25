"""Ollama-native backend adapter.

Ollama's ``/api/chat`` streams **newline-delimited JSON** (``application/x-ndjson``),
one JSON object per line, terminated by an object with ``done: true`` -- not SSE. So
this adapter frames it with the shared, line-capped :func:`._sse.async_iter_lines`
(``_iter_ndjson`` below adds the JSON decode). It is a **stateless-replay** adapter: every turn it rebuilds the provider
``messages[]`` from ``chat_log.content`` and trims old rounds via the shared
``_history.trim_history`` helper (core-ollama's ``_trim_history`` rule).

Template: HA core ``homeassistant/components/ollama/entity.py`` (``_convert_content``,
``_transform_stream``, ``_trim_history``) and ``config_flow.py`` (the option set). We
speak raw ``aiohttp`` rather than the ``ollama`` pip client to keep the manifest deps
thin.

Tools (LLMM-015): when ``chat_log.llm_api`` is set the adapter formats HA tool schemas
into the request ``tools`` field, parses native ``message.tool_calls`` from the NDJSON
stream into ``llm.ToolInput`` deltas (repairing small-model malformed args via
``_parse_tool_args``), and replays prior tool calls/results back into ``messages[]`` on
the next iteration. The entity's bounded tool loop drives the re-entry; this adapter
stays stateless.

Tier-1 (LLMM-024): tool calls keep Ollama's ``id`` and replay with ``function.index``, and
tool results carry ``tool_name`` + ``tool_call_id`` (``api/types.go`` ``Message``). A
mid-stream ``{"error": …}`` line (HTTP status already 200) raises
:class:`BackendStreamError`; ``done_reason: "length"`` keeps the text and is logged +
traced. Empty ``num_ctx`` / ``keep_alive`` options send core HA's defaults; ``think`` is a
bool or a level.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, AsyncIterable, Mapping, Sequence
from typing import Any, ClassVar, cast

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.ulid import ulid_now

from custom_components.llm_middleman.const import (
    BACKEND_OLLAMA,
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_KEEP_ALIVE,
    CONF_MAX_HISTORY,
    CONF_MODEL,
    CONF_NUM_CTX,
    CONF_THINK,
    DEFAULT_KEEP_ALIVE,
    DEFAULT_NUM_CTX,
    KEEP_ALIVE_FOREVER,
    THINK_OFF,
    THINK_ON,
)

from ._history import trim_history
from ._sse import async_iter_lines
from ._tools import EncodedImage, async_load_images, format_function_tool, replayable_content, tool_result_json
from .base import (
    BackendAdapter,
    BackendConnectionError,
    BackendStreamError,
    DeltaStream,
    TurnContext,
    bearer_headers,
    build_client_timeout,
    json_object,
    probe_json,
    record_truncation,
    trace_token_stats,
)

_LOGGER = logging.getLogger(__name__)

_LABEL = "Ollama"


def _fix_invalid_arguments(value: Any) -> Any:
    """Repair one malformed tool-argument value (core ``ollama/entity.py``).

    Small local models routinely emit a JSON array/object as a *string*; parse it back
    when it looks like one, else pass the value through unchanged.
    """
    if not isinstance(value, str):
        return value
    if (value.startswith("[") and value.endswith("]")) or (value.startswith("{") and value.endswith("}")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def _parse_tool_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Repair small-model tool-argument dicts (core ``ollama/entity.py``).

    Drops keys whose value is ``None`` or ``""`` (they fail HA intent parsing) and
    parses any stringified-JSON value back into a list/dict.
    """
    return {k: _fix_invalid_arguments(v) for k, v in arguments.items() if v is not None and v != ""}


def _think(value: object) -> bool | str:
    """Map the stored ``think`` option to the wire value: a bool, or a level string.

    Agents saved before levels existed store a bool; the selector stores ``on``/``off``.
    """
    if value is True or value == THINK_ON:
        return True
    if value is False or value == THINK_OFF:
        return False
    return str(value)


def _convert_content(content: conversation.Content, images: Sequence[EncodedImage] = ()) -> dict[str, Any]:
    """Map one ChatLog item to an Ollama ``messages[]`` entry.

    Assistant turns carry any ``tool_calls`` (LLMM-015) so a re-entered tool loop
    replays them in Ollama's ``{"function": {"name", "arguments"}}`` wire shape; the
    closed ``Content`` union makes the final branch the tool-result case, serialized
    with ``default=str`` (a ``time``/``datetime`` tool result is not otherwise
    JSON-serializable). A user turn's ``images`` (AI Task attachments, already
    encoded) go in the message's base64 ``images`` list.
    """
    if isinstance(content, conversation.SystemContent):
        return {"role": "system", "content": content.content}
    if isinstance(content, conversation.UserContent):
        user: dict[str, Any] = {"role": "user", "content": content.content}
        if images:
            user["images"] = [data for _mime_type, data in images]
        return user
    if isinstance(content, conversation.AssistantContent):
        message: dict[str, Any] = {"role": "assistant", "content": content.content or ""}
        if content.thinking_content:
            message["thinking"] = content.thinking_content
        if content.tool_calls:
            message["tool_calls"] = [
                {"id": call.id, "function": {"index": index, "name": call.tool_name, "arguments": call.tool_args}}
                for index, call in enumerate(content.tool_calls)
            ]
        return message
    # Only ToolResultContent remains (Content is a closed union).
    return {
        "role": "tool",
        "content": tool_result_json(content),
        "tool_name": content.tool_name,
        "tool_call_id": content.tool_call_id,
    }


async def _iter_ndjson(stream: AsyncIterable[bytes]) -> AsyncGenerator[dict[str, Any]]:
    """Decode a raw NDJSON byte stream into JSON objects, one per line.

    Lines come from :func:`async_iter_lines` (chunk-boundary agnostic, EOF tail
    flushed, oversize lines skipped). Blank lines are skipped. A line that is not
    a valid JSON object raises ``BackendStreamError`` so the entity guard can map it to the
    fallback (never let a raw ``JSONDecodeError`` escape).
    """
    async for line in async_iter_lines(stream):
        if not line.strip():
            continue
        try:
            obj = json_object(json.loads(line))
        except json.JSONDecodeError as err:
            raise BackendStreamError(f"Malformed NDJSON line: {line!r}") from err
        if obj is None:
            raise BackendStreamError(f"NDJSON line is not an object: {line!r}")
        yield obj


class OllamaAdapter(BackendAdapter):
    """Ollama-native preset: ``/api/chat`` NDJSON, stateless replay + trim."""

    backend_type: ClassVar[str] = BACKEND_OLLAMA
    # Formats HA tool schemas + parses native message.tool_calls with _parse_tool_args
    # repair (LLMM-015), so the subentry flow offers CONF_LLM_HASS_API and the entity
    # runs the tool loop.
    supports_ha_tools: ClassVar[bool] = True
    # Replays chat_log (whose system prompt carries HA's date/time); never reads user_input.text.
    wants_time_prefix: ClassVar[bool] = False
    # Sends TurnContext.output_schema as ``format`` and images in ``images`` (LLMM-023).
    supports_ai_task: ClassVar[bool] = True

    @classmethod
    async def _async_get_models(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Probe ``GET /api/tags`` and return the installed-model objects.

        Raises :class:`BackendAuthError` on 401/403 and :class:`BackendConnectionError`
        on any other failure (bad status, timeout, transport error, non-object body).
        """
        payload = await probe_json(
            async_get_clientsession(hass),
            f"{data[CONF_BASE_URL].rstrip('/')}/api/tags",
            label="Ollama",
            headers=bearer_headers(data.get(CONF_API_KEY)),
        )
        return cast("list[dict[str, Any]]", payload.get("models", []))

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        """Probe ``GET /api/tags``; return ``None``, raise on failure."""
        await cls._async_get_models(hass, data)

    @classmethod
    async def async_list_models(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> list[str] | None:
        """Installed-model list for the subentry model dropdown (from ``/api/tags``)."""
        models = await cls._async_get_models(hass, data)
        return [model["model"] for model in models]

    async def _build_request(self, chat_log: conversation.ChatLog, ctx: TurnContext) -> dict[str, Any]:
        """Rebuild the trimmed provider ``messages[]`` and assemble the request body.

        ``num_ctx`` / ``keep_alive`` fall back to core HA's defaults when unset; ``think``
        is sent only when configured. ``base_url`` is the host root (not ``/v1``); the
        caller strips the trailing slash before appending paths.
        """
        options = ctx.options
        items = replayable_content(chat_log.content)
        images = await async_load_images(self.hass, items)
        messages = trim_history(
            [_convert_content(item, item_images) for item, item_images in zip(items, images, strict=True)],
            int(options.get(CONF_MAX_HISTORY, 0)),
        )
        body: dict[str, Any] = {"messages": messages, "stream": True}
        if CONF_MODEL in options:
            body["model"] = options[CONF_MODEL]
        # int(): HA's NumberSelector stores floats (core ollama sends int(num_ctx) too).
        body["options"] = {"num_ctx": int(options.get(CONF_NUM_CTX, DEFAULT_NUM_CTX))}
        keep_alive = int(options.get(CONF_KEEP_ALIVE, DEFAULT_KEEP_ALIVE))
        body["keep_alive"] = keep_alive if keep_alive == KEEP_ALIVE_FOREVER else f"{keep_alive}s"
        if CONF_THINK in options:
            body["think"] = _think(options[CONF_THINK])
        # HA tool schemas, only when an LLM API is configured for this agent.
        if chat_log.llm_api is not None and chat_log.llm_api.tools:
            body["tools"] = [
                format_function_tool(tool, chat_log.llm_api.custom_serializer, omit_empty_description=True)
                for tool in chat_log.llm_api.tools
            ]
        # AI Task structured output (Ollama structured outputs: a JSON Schema in ``format``).
        if ctx.output_schema is not None:
            body["format"] = ctx.output_schema.schema
        return body

    async def stream_turn(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        ctx: TurnContext,
    ) -> DeltaStream:
        """Replay trimmed history to ``/api/chat`` and stream role-first deltas.

        Stateless: rebuilds messages from ``chat_log.content`` each turn and ignores
        ``ctx.memory_key``. Emits ``{"role": "assistant"}`` once before the first
        non-empty content (or thinking) delta, then one delta per object. Whitespace
        in content is preserved verbatim. An ``{"error": …}`` line raises after any text
        already streamed. Stops on ``done: true`` (tracing its token counts as ``stats``
        when present, and a ``length`` cut-off); on a silent EOF
        the generator simply ends and LLMM-005's entity guard supplies the final
        ``AssistantContent``.
        """
        base_url = self.connection_data[CONF_BASE_URL].rstrip("/")
        body = await self._build_request(chat_log, ctx)

        async with self._post_checked(
            f"{base_url}/api/chat",
            label="Ollama /api/chat",
            json=body,
            headers=bearer_headers(self.connection_data.get(CONF_API_KEY)),
            timeout=build_client_timeout(ctx.options),
            error_cls=BackendConnectionError,
        ) as response:
            role_emitted = False
            async for obj in _iter_ndjson(response.content.iter_any()):
                if (error := obj.get("error")) is not None:
                    _LOGGER.error("%s stream error: %s", _LABEL, error)
                    raise BackendStreamError(f"{_LABEL} stream error: {error}")
                message: dict[str, Any] = obj.get("message") or {}
                tool_calls = message.get("tool_calls")
                if tool_calls:
                    if not role_emitted:
                        role_emitted = True
                        yield {"role": "assistant"}
                    # Ollama emits each call as a whole object with args already a dict
                    # (not a JSON string) -> no json.loads, just the malformed-arg repair.
                    yield {
                        "tool_calls": [
                            # Keep Ollama's id (newer servers send one) so the result replays
                            # with a matching tool_call_id; else mint one as ToolInput would.
                            llm.ToolInput(
                                id=tc.get("id") or ulid_now(),
                                tool_name=tc["function"]["name"],
                                tool_args=_parse_tool_args(tc["function"]["arguments"]),
                            )
                            for tc in tool_calls
                        ]
                    }
                thinking = message.get("thinking")
                if thinking:
                    if not role_emitted:
                        role_emitted = True
                        yield {"role": "assistant"}
                    yield {"thinking_content": thinking}
                content = message.get("content")
                if content:
                    if not role_emitted:
                        role_emitted = True
                        yield {"role": "assistant"}
                    yield {"content": content}
                if obj.get("done"):
                    if obj.get("done_reason") == "length":
                        record_truncation(chat_log, _LABEL, "length")
                    # The terminal frame carries the round-trip's token counts;
                    # prompt_eval_count is omitted when the prompt was fully cached.
                    if "prompt_eval_count" in obj or "eval_count" in obj:
                        cached = obj.get("prompt_eval_cached_count")
                        trace_token_stats(
                            chat_log,
                            input_tokens=int(obj.get("prompt_eval_count") or 0),
                            cached_input_tokens=cached if isinstance(cached, int) else None,
                            output_tokens=int(obj.get("eval_count") or 0),
                        )
                    break
