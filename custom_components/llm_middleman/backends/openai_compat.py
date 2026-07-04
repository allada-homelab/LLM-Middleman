"""OpenAI-compatible backend adapter (``/v1/chat/completions``, text + tools).

The default preset: one adapter reaches OpenAI, vLLM, LocalAI, LM Studio,
llama.cpp-server, Ollama's ``/v1`` shim, OpenRouter, Groq, together, etc. It is a
**stateless-replay** adapter — every turn rebuilds the provider ``messages[]`` from
``chat_log.content`` (trimmed via ``CONF_MAX_HISTORY``) and streams the reply as
canonical HA delta dicts through the shared spec-compliant SSE reader.

Wire format is chat/completions (``choices[].delta.content`` + a literal
``data: [DONE]`` sentinel), not the newer Responses API core-openai targets.

Tools (LLMM-014): when ``chat_log.llm_api`` is set the adapter formats HA tool
schemas into the request ``tools`` field, accumulates streamed
``choices[].delta.tool_calls`` fragments by ``index``, emits ``llm.ToolInput`` deltas,
and replays prior tool calls/results back into ``messages[]`` on the next iteration.
The entity's bounded tool loop drives the re-entry; this adapter stays stateless.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import aiohttp
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from voluptuous_openapi import convert  # pyright: ignore[reportUnknownVariableType]

from custom_components.llm_middleman.const import (
    BACKEND_OPENAI_COMPAT,
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MAX_HISTORY,
    CONF_MAX_TOKENS,
    CONF_MODEL,
    CONF_TEMPERATURE,
    CONF_TOP_P,
    DEFAULT_TIMEOUT,
)

from ._history import trim_history
from ._sse import async_iter_sse
from .base import (
    BackendAdapter,
    BackendAuthError,
    BackendConnectionError,
    DeltaStream,
    TurnContext,
    build_client_timeout,
)

# The provider terminates the SSE stream with this literal payload, not JSON.
_DONE_SENTINEL = "[DONE]"

# Request-body option keys read from the subentry options, sent only when set.
_OPTION_KEYS = (CONF_TEMPERATURE, CONF_TOP_P, CONF_MAX_TOKENS)


def _base_url(data: Mapping[str, Any]) -> str:
    """Return the connection base URL with any trailing slash stripped.

    A trailing ``/`` double-slashes the path and 404s ``/v1/models`` /
    ``/v1/chat/completions``; the parent flow normalizes it, but strip defensively.
    """
    return str(data[CONF_BASE_URL]).rstrip("/")


def _auth_headers(data: Mapping[str, Any]) -> dict[str, str]:
    """Return the Bearer auth header when an api_key is configured, else empty.

    ``api_key`` is optional (many compatible servers accept a dummy value); the
    adapter simply sends the header only when a non-empty key is present.
    """
    api_key = data.get(CONF_API_KEY)
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _format_tool(tool: llm.Tool, custom_serializer: Callable[[Any], Any] | None) -> dict[str, Any]:
    """Format one HA tool as an OpenAI chat/completions function-tool spec.

    Chat/completions nests the spec under ``function`` (not the flat Responses shape
    core openai uses). ``convert`` (voluptuous-openapi) renders the voluptuous schema
    to JSON Schema; ``custom_serializer`` handles HA-specific selector types. The
    unsupported-JSON-Schema-key strip core applies is intentionally omitted: most
    OpenAI-compatible servers tolerate ``enum``/``anyOf``/… and stripping them silently
    degrades the schema — add it only if a target server actually 400s (LLMM-018 probe).
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": convert(tool.parameters, custom_serializer=custom_serializer),
        },
    }


def _accumulate_tool_call(tool_calls: dict[int, dict[str, str]], fragment: Any) -> None:
    """Fold one streamed ``delta.tool_calls`` fragment into the by-index accumulator.

    ``id``/``function.name`` appear on the first fragment for an index;
    ``function.arguments`` streams as a string concatenated across fragments. Missing
    fields are treated as empty (``get(...) or ""``) so a fragment carrying only more
    argument text folds in cleanly. Absent ``index`` defaults to 0 (single-call servers).
    """
    index: Any = fragment.get("index", 0) if hasattr(fragment, "get") else 0
    slot = tool_calls.setdefault(int(index), {"id": "", "name": "", "args": ""})
    slot["id"] += fragment.get("id") or ""
    function: Any = fragment.get("function") if hasattr(fragment, "get") else None
    if hasattr(function, "get"):
        slot["name"] += function.get("name") or ""
        slot["args"] += function.get("arguments") or ""


def _convert_content(item: conversation.Content) -> dict[str, Any]:
    """Map one HA ChatLog content item to an OpenAI chat message.

    Assistant turns carry any ``tool_calls`` (LLMM-014) so a re-entered tool loop
    replays them; the closed ``Content`` union makes the final branch the tool-result
    case, serialized with ``default=str`` (a ``time``/``datetime`` tool result is not
    otherwise JSON-serializable).
    """
    if isinstance(item, conversation.SystemContent):
        return {"role": "system", "content": item.content}
    if isinstance(item, conversation.UserContent):
        return {"role": "user", "content": item.content}
    if isinstance(item, conversation.AssistantContent):
        message: dict[str, Any] = {"role": "assistant", "content": item.content or ""}
        if item.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.tool_name, "arguments": json.dumps(call.tool_args)},
                }
                for call in item.tool_calls
            ]
        return message
    return {
        "role": "tool",
        "tool_call_id": item.tool_call_id,
        "content": json.dumps(item.tool_result, default=str),
    }


class OpenAICompatAdapter(BackendAdapter):
    """Stateless-replay adapter for OpenAI-compatible ``/v1/chat/completions``."""

    backend_type: ClassVar[str] = BACKEND_OPENAI_COMPAT
    # Formats HA tool schemas + parses streamed tool_calls (LLMM-014), so the subentry
    # flow offers CONF_LLM_HASS_API and the entity runs the tool loop.
    supports_ha_tools: ClassVar[bool] = True

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        """Probe ``GET /v1/models``; raise typed errors on failure, return None on 200."""
        await cls._fetch_models(hass, data)

    @classmethod
    async def async_list_models(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> list[str] | None:
        """Return the model-id list from ``GET /v1/models`` for the subentry dropdown."""
        return await cls._fetch_models(hass, data)

    @classmethod
    async def _fetch_models(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> list[str]:
        """GET ``/v1/models`` and return the parsed model-id list.

        Maps 401/403 → :class:`BackendAuthError`, any other non-200 and every
        transport/timeout failure → :class:`BackendConnectionError`.
        """
        session = async_get_clientsession(hass)
        url = f"{_base_url(data)}/v1/models"
        try:
            async with (
                asyncio.timeout(DEFAULT_TIMEOUT),
                session.get(url, headers=_auth_headers(data)) as response,
            ):
                if response.status in (401, 403):
                    raise BackendAuthError(f"Backend rejected credentials (HTTP {response.status})")
                if response.status != 200:
                    raise BackendConnectionError(f"Model probe failed (HTTP {response.status})")
                payload = await response.json()
        except BackendConnectionError:
            raise
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BackendConnectionError(f"Cannot reach backend at {url}") from err
        # response.json() is Any; access via Any (isinstance-narrowing a dict yields
        # dict[Unknown, Unknown] under strict mode). hasattr guards a non-object body.
        entries: Any = payload.get("data", []) if hasattr(payload, "get") else []
        models: list[str] = []
        for entry in entries:
            entry_id: Any = entry.get("id") if hasattr(entry, "get") else None
            if isinstance(entry_id, str):
                models.append(entry_id)
        return models

    def _build_messages(self, chat_log: conversation.ChatLog, ctx: TurnContext) -> list[dict[str, Any]]:
        """Replay ``chat_log.content`` to trimmed OpenAI ``messages[]``."""
        messages = [_convert_content(item) for item in chat_log.content]
        max_history = int(ctx.options.get(CONF_MAX_HISTORY, 0))
        return trim_history(messages, max_history)

    def _build_body(self, chat_log: conversation.ChatLog, ctx: TurnContext) -> dict[str, Any]:
        """Assemble the chat/completions request body from history + options."""
        body: dict[str, Any] = {
            "messages": self._build_messages(chat_log, ctx),
            "stream": True,
        }
        if (model := ctx.options.get(CONF_MODEL)) is not None:
            body["model"] = model
        for key in _OPTION_KEYS:
            if (value := ctx.options.get(key)) is not None:
                body[key] = value
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
        """POST the replayed turn with ``stream: true`` and yield HA delta dicts.

        Emits ``{"role": "assistant"}`` once before the first content-bearing (or
        tool-call) delta — role-first is required; a role-less delta is treated as a
        continuation — then ``{"content": …}`` per fragment verbatim (whitespace and
        empty strings pass through untrimmed). Streamed ``tool_calls`` fragments are
        accumulated by ``index`` across chunks and flushed as one ``{"tool_calls":
        [llm.ToolInput, …]}`` delta after ``[DONE]`` / EOF, so HA can run the tools and
        the entity's loop re-enters. ``[DONE]`` ends the stream; EOF without it also
        ends it (the entity guard supplies the fallback when nothing streamed).
        """
        headers = {"Accept": "text/event-stream", **_auth_headers(self.connection_data)}
        url = f"{_base_url(self.connection_data)}/v1/chat/completions"
        role_sent = False
        # index -> partial call; id/name arrive on the first fragment, arguments stream
        # as a string concatenated across fragments (chat/completions delta.tool_calls).
        tool_calls: dict[int, dict[str, str]] = {}
        async with self.session.post(
            url,
            json=self._build_body(chat_log, ctx),
            headers=headers,
            timeout=build_client_timeout(ctx.options),
        ) as response:
            async for event in async_iter_sse(response.content.iter_any()):
                if event.data == _DONE_SENTINEL:
                    break
                # json.loads is Any; access via Any (isinstance-narrowing a dict
                # yields dict[Unknown, Unknown] under strict mode). A non-conforming
                # shape raises here and the entity's _guarded wrapper maps it to the
                # fallback. hasattr guards the ``.get`` against non-object chunks.
                chunk: Any = json.loads(event.data)
                choices: Any = chunk.get("choices") if hasattr(chunk, "get") else None
                if not choices:
                    continue
                delta: Any = choices[0].get("delta")
                fragments: Any = delta.get("tool_calls") if hasattr(delta, "get") else None
                if fragments:
                    for fragment in fragments:
                        _accumulate_tool_call(tool_calls, fragment)
                content: Any = delta.get("content") if hasattr(delta, "get") else None
                if content is None:
                    continue
                if not role_sent:
                    yield {"role": "assistant"}
                    role_sent = True
                yield {"content": content}
        # Flush accumulated tool calls once, after [DONE]/EOF (some servers omit the id
        # on later fragments and/or the finish_reason, so index-keyed accumulation +
        # a single terminal flush is the tolerant contract).
        if tool_calls:
            if not role_sent:
                yield {"role": "assistant"}
                role_sent = True
            yield {
                "tool_calls": [
                    llm.ToolInput(
                        id=call["id"],
                        tool_name=call["name"],
                        tool_args=json.loads(call["args"] or "{}"),
                    )
                    for call in tool_calls.values()
                ]
            }
