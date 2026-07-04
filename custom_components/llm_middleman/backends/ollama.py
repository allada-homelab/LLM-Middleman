"""Ollama-native backend adapter.

Ollama's ``/api/chat`` streams **newline-delimited JSON** (``application/x-ndjson``),
one JSON object per line, terminated by an object with ``done: true`` -- not SSE. So
this adapter frames the stream itself (``_iter_ndjson`` below) instead of using
``_sse.py``. It is a **stateless-replay** adapter: every turn it rebuilds the provider
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
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterable, Callable, Mapping
from typing import Any, ClassVar, cast

import aiohttp
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from voluptuous_openapi import convert  # pyright: ignore[reportUnknownVariableType]

from custom_components.llm_middleman.const import (
    BACKEND_OLLAMA,
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_KEEP_ALIVE,
    CONF_MAX_HISTORY,
    CONF_MODEL,
    CONF_NUM_CTX,
    CONF_THINK,
    DEFAULT_TIMEOUT,
    KEEP_ALIVE_FOREVER,
)

from ._history import trim_history
from .base import (
    BackendAdapter,
    BackendAuthError,
    BackendConnectionError,
    BackendStreamError,
    DeltaStream,
    TurnContext,
    build_client_timeout,
)


def _auth_headers(data: Mapping[str, Any]) -> dict[str, str]:
    """Bearer header when an API key is configured; usually absent on a LAN Ollama."""
    api_key = data.get(CONF_API_KEY)
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _format_tool(tool: llm.Tool, custom_serializer: Callable[[Any], Any] | None) -> dict[str, Any]:
    """Format one HA tool as an Ollama ``/api/chat`` function-tool spec.

    Ollama's wire shape nests ``name``/``parameters``/``description`` under ``function``
    and, unlike OpenAI's, omits ``description`` when the tool has none (core
    ``ollama/entity.py:_format_tool``). ``convert`` (voluptuous-openapi) renders the
    voluptuous schema to JSON Schema; ``custom_serializer`` handles HA selector types.
    """
    function: dict[str, Any] = {
        "name": tool.name,
        "parameters": convert(tool.parameters, custom_serializer=custom_serializer),
    }
    if tool.description:
        function["description"] = tool.description
    return {"type": "function", "function": function}


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


def _convert_content(content: conversation.Content) -> dict[str, Any]:
    """Map one ChatLog item to an Ollama ``messages[]`` entry.

    Assistant turns carry any ``tool_calls`` (LLMM-015) so a re-entered tool loop
    replays them in Ollama's ``{"function": {"name", "arguments"}}`` wire shape; the
    closed ``Content`` union makes the final branch the tool-result case, serialized
    with ``default=str`` (a ``time``/``datetime`` tool result is not otherwise
    JSON-serializable).
    """
    if isinstance(content, conversation.SystemContent):
        return {"role": "system", "content": content.content}
    if isinstance(content, conversation.UserContent):
        return {"role": "user", "content": content.content}
    if isinstance(content, conversation.AssistantContent):
        message: dict[str, Any] = {"role": "assistant", "content": content.content or ""}
        if content.thinking_content:
            message["thinking"] = content.thinking_content
        if content.tool_calls:
            message["tool_calls"] = [
                {"function": {"name": call.tool_name, "arguments": call.tool_args}} for call in content.tool_calls
            ]
        return message
    # Only ToolResultContent remains (Content is a closed union).
    return {"role": "tool", "content": json.dumps(content.tool_result, default=str)}


async def _iter_ndjson(stream: AsyncIterable[bytes]) -> AsyncGenerator[dict[str, Any]]:
    """Frame a raw NDJSON byte stream into JSON objects, one per line.

    Maintains a byte buffer, splits on ``\\n``, and ``json.loads`` each complete
    line; the trailing partial fragment is carried to the next chunk, and a final
    object with no trailing newline is flushed at EOF. Boundaries need not align
    with lines. Blank lines are skipped. A line that is not valid JSON raises
    ``BackendStreamError`` so the entity guard can map it to the fallback (never let
    a raw ``JSONDecodeError`` escape).
    """
    buffer = bytearray()

    def _load(raw: bytes) -> dict[str, Any]:
        try:
            return cast("dict[str, Any]", json.loads(raw))
        except json.JSONDecodeError as err:
            raise BackendStreamError(f"Malformed NDJSON line: {raw!r}") from err

    async for chunk in stream:
        buffer.extend(chunk)
        while (newline := buffer.find(b"\n")) != -1:
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if line.strip():
                yield _load(line)

    tail = bytes(buffer).strip()
    if tail:
        yield _load(tail)


class OllamaAdapter(BackendAdapter):
    """Ollama-native preset: ``/api/chat`` NDJSON, stateless replay + trim."""

    backend_type: ClassVar[str] = BACKEND_OLLAMA
    # Formats HA tool schemas + parses native message.tool_calls with _parse_tool_args
    # repair (LLMM-015), so the subentry flow offers CONF_LLM_HASS_API and the entity
    # runs the tool loop.
    supports_ha_tools: ClassVar[bool] = True

    @classmethod
    async def _async_get_models(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Probe ``GET /api/tags`` and return the installed-model objects.

        Raises :class:`BackendAuthError` on 401/403 and :class:`BackendConnectionError`
        on any other failure (bad status, timeout, transport error).
        """
        session = async_get_clientsession(hass)
        base_url = data[CONF_BASE_URL].rstrip("/")
        try:
            async with (
                asyncio.timeout(DEFAULT_TIMEOUT),
                session.get(f"{base_url}/api/tags", headers=_auth_headers(data)) as response,
            ):
                if response.status in (401, 403):
                    raise BackendAuthError(f"Ollama rejected credentials (HTTP {response.status})")
                if response.status != 200:
                    raise BackendConnectionError(f"Ollama /api/tags returned HTTP {response.status}")
                payload = await response.json()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BackendConnectionError(f"Could not reach Ollama at {base_url}: {err}") from err
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

    def _build_request(self, chat_log: conversation.ChatLog, options: Mapping[str, Any]) -> dict[str, Any]:
        """Rebuild the trimmed provider ``messages[]`` and assemble the request body.

        Each option is included only when configured. ``base_url`` is the host root
        (not ``/v1``); the caller strips the trailing slash before appending paths.
        """
        messages = trim_history(
            [_convert_content(content) for content in chat_log.content],
            int(options.get(CONF_MAX_HISTORY, 0)),
        )
        body: dict[str, Any] = {"messages": messages, "stream": True}
        if CONF_MODEL in options:
            body["model"] = options[CONF_MODEL]
        if CONF_NUM_CTX in options:
            body["options"] = {"num_ctx": options[CONF_NUM_CTX]}
        if CONF_KEEP_ALIVE in options:
            keep_alive = int(options[CONF_KEEP_ALIVE])
            body["keep_alive"] = keep_alive if keep_alive == KEEP_ALIVE_FOREVER else f"{keep_alive}s"
        if CONF_THINK in options:
            body["think"] = options[CONF_THINK]
        # HA tool schemas, only when an LLM API is configured for this agent.
        if chat_log.llm_api is not None and chat_log.llm_api.tools:
            body["tools"] = [_format_tool(tool, chat_log.llm_api.custom_serializer) for tool in chat_log.llm_api.tools]
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
        in content is preserved verbatim. Stops on ``done: true``; on a silent EOF
        the generator simply ends and LLMM-005's entity guard supplies the final
        ``AssistantContent``.
        """
        base_url = self.connection_data[CONF_BASE_URL].rstrip("/")
        body = self._build_request(chat_log, ctx.options)

        async with self.session.post(
            f"{base_url}/api/chat",
            json=body,
            headers=_auth_headers(self.connection_data),
            timeout=build_client_timeout(ctx.options),
        ) as response:
            if response.status != 200:
                raise BackendConnectionError(f"Ollama /api/chat returned HTTP {response.status}")

            role_emitted = False
            async for obj in _iter_ndjson(response.content.iter_any()):
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
                            llm.ToolInput(
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
                    break
