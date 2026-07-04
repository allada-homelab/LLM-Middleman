"""OpenAI-compatible backend adapter (``/v1/chat/completions``, text-only).

The default preset: one adapter reaches OpenAI, vLLM, LocalAI, LM Studio,
llama.cpp-server, Ollama's ``/v1`` shim, OpenRouter, Groq, together, etc. It is a
**stateless-replay** adapter — every turn rebuilds the provider ``messages[]`` from
``chat_log.content`` (trimmed via ``CONF_MAX_HISTORY``) and streams the reply as
canonical HA delta dicts through the shared spec-compliant SSE reader.

Wire format is chat/completions (``choices[].delta.content`` + a literal
``data: [DONE]`` sentinel), not the newer Responses API core-openai targets.

Scope is **text-only** (LLMM-008): ``supports_ha_tools = False`` and no tool-schema
passing. LLMM-014 flips the flag and accumulates ``choices[].delta.tool_calls``
fragments at the marked seam without reshaping this loop.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, ClassVar

import aiohttp
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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


def _convert_content(item: conversation.Content) -> dict[str, Any]:
    """Map one HA ChatLog content item to an OpenAI chat message.

    Text-only subset; ``ToolResultContent`` is mapped now (it cannot appear until
    LLMM-014 wires tools) so history replay needs no change when tools land. The
    ``Content`` union is closed, so the final branch is the tool-result case.
    """
    if isinstance(item, conversation.SystemContent):
        return {"role": "system", "content": item.content}
    if isinstance(item, conversation.UserContent):
        return {"role": "user", "content": item.content}
    if isinstance(item, conversation.AssistantContent):
        return {"role": "assistant", "content": item.content or ""}
    return {
        "role": "tool",
        "tool_call_id": item.tool_call_id,
        "content": json.dumps(item.tool_result, default=str),
    }


class OpenAICompatAdapter(BackendAdapter):
    """Stateless-replay adapter for OpenAI-compatible ``/v1/chat/completions``."""

    backend_type: ClassVar[str] = BACKEND_OPENAI_COMPAT
    # Tool loop lands in LLMM-014; keep False so the subentry flow does not offer a
    # tool option that does nothing.
    supports_ha_tools: ClassVar[bool] = False

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
        return body

    async def stream_turn(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        ctx: TurnContext,
    ) -> DeltaStream:
        """POST the replayed turn with ``stream: true`` and yield HA delta dicts.

        Emits ``{"role": "assistant"}`` once before the first content-bearing delta
        (role-first is required; a role-less delta is treated as a continuation),
        then ``{"content": …}`` per fragment verbatim — whitespace and empty strings
        pass through untrimmed. ``[DONE]`` ends the stream; EOF without it also ends
        it (the entity guard supplies the fallback when nothing streamed).
        """
        headers = {"Accept": "text/event-stream", **_auth_headers(self.connection_data)}
        url = f"{_base_url(self.connection_data)}/v1/chat/completions"
        role_sent = False
        async with self.session.post(
            url,
            json=self._build_body(chat_log, ctx),
            headers=headers,
            timeout=build_client_timeout(ctx.options),
        ) as response:
            async for event in async_iter_sse(response.content.iter_any()):
                if event.data == _DONE_SENTINEL:
                    return
                # json.loads is Any; access via Any (isinstance-narrowing a dict
                # yields dict[Unknown, Unknown] under strict mode). A non-conforming
                # shape raises here and the entity's _guarded wrapper maps it to the
                # fallback. hasattr guards the ``.get`` against non-object chunks.
                chunk: Any = json.loads(event.data)
                choices: Any = chunk.get("choices") if hasattr(chunk, "get") else None
                if not choices:
                    continue
                delta: Any = choices[0].get("delta")
                # tool_calls: LLMM-014 accumulates delta["tool_calls"] fragments here.
                content: Any = delta.get("content") if hasattr(delta, "get") else None
                if content is None:
                    continue
                if not role_sent:
                    yield {"role": "assistant"}
                    role_sent = True
                yield {"content": content}
