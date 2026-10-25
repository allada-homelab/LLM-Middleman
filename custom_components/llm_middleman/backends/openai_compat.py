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

Token stats: requests ``stream_options.include_usage`` and traces the terminal ``usage``
chunk as HA's ``{"stats": …}`` agent detail. Streamed ``delta.reasoning_content`` /
``delta.reasoning`` map to HA ``thinking_content`` (never spoken); ``delta.refusal`` is
spoken like content.

Terminal states (LLMM-024): an in-stream ``error`` object (vLLM, OpenRouter) or
``finish_reason: "error"`` raises :class:`BackendStreamError`; ``length`` /
``content_filter`` keep the text and are logged + traced. Request fields some servers
refuse are retried: ``stream_options`` / ``prompt_cache_key`` are dropped for the
connection, and ``max_tokens`` becomes ``max_completion_tokens`` for a model that rejects
it (OpenAI reasoning models). HTTP 429 raises the translated ``backend_rate_limited`` error.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import aiohttp
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.llm_middleman.const import (
    BACKEND_OPENAI_COMPAT,
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MAX_HISTORY,
    CONF_MAX_TOKENS,
    CONF_MODEL,
    CONF_REASONING_EFFORT,
    CONF_TEMPERATURE,
    CONF_TOP_P,
    DOMAIN,
)

from ._history import trim_history
from ._sse import async_iter_sse
from ._tools import EncodedImage, async_load_images, format_function_tool, replayable_content, tool_result_json
from .base import (
    BackendAdapter,
    BackendStreamError,
    DeltaStream,
    TurnContext,
    bearer_headers,
    build_client_timeout,
    json_object,
    loads_json_object,
    probe_json,
    record_truncation,
    trace_token_stats,
)

_LOGGER = logging.getLogger(__name__)

# The provider terminates the SSE stream with this literal payload, not JSON.
_DONE_SENTINEL = "[DONE]"

# Request-body option keys read from the subentry options, sent only when set; the
# converter undoes HA's NumberSelector float storage where the API wants an integer.
_OPTION_KEYS: tuple[tuple[str, type[float] | type[int]], ...] = (
    (CONF_TEMPERATURE, float),
    (CONF_TOP_P, float),
    (CONF_MAX_TOKENS, int),
)

_LABEL = "OpenAI-compatible backend"

# Asks OpenAI-style servers for a terminal ``usage`` chunk on streamed responses.
_STREAM_OPTIONS = {"include_usage": True}

# Optional request fields a strict server may refuse by name (old Azure, Groq, Mistral…);
# once refused, a field is dropped for the connection.
_DROPPABLE_FIELDS = ("stream_options", "prompt_cache_key")

# finish_reason values that cut the reply short but leave usable text.
_TRUNCATED = ("length", "content_filter")


class _FieldsRejectedError(Exception):
    """The server refused the request because of the named retryable fields."""

    def __init__(self, fields: frozenset[str]) -> None:
        super().__init__(", ".join(sorted(fields)))
        self.fields = fields


def _rejected_fields(request: Mapping[str, Any], status: int, text: str) -> frozenset[str]:
    """Return the retryable request fields a 400/422 error body names (empty: not retryable)."""
    if status not in (400, 422):
        return frozenset()
    fields = {name for name in _DROPPABLE_FIELDS if name in request and name in text}
    # Reasoning models refuse max_tokens as an "unsupported parameter"; vLLM also names it in
    # "max_tokens is too large", which a retry with max_completion_tokens would not fix.
    if "max_tokens" in request and "max_tokens" in text and "support" in text.lower():
        fields.add("max_tokens")
    return frozenset(fields)


def _error_message(error: object, fallback: str) -> str:
    """The ``message`` of an OpenAI-style error object, a bare error string, or ``fallback``."""
    if isinstance(error, str) and error:
        return error
    message = (json_object(error) or {}).get("message")
    return str(message) if message else fallback


async def _check_error_status(request: Mapping[str, Any], response: aiohttp.ClientResponse) -> None:
    """``_post_checked`` hook: 429 → translated rate-limit error; a refused field → retry."""
    text = await response.text()
    if response.status == 429:
        message = _error_message((loads_json_object(text) or {}).get("error"), text[:200])
        _LOGGER.error("%s returned HTTP 429: %s", _LABEL, message)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="backend_rate_limited",
            translation_placeholders={"error": message},
        )
    if fields := _rejected_fields(request, response.status, text):
        raise _FieldsRejectedError(fields)


def _raise_stream_error(error: object) -> None:
    """Raise for a backend-sent in-stream error (vLLM ``{"error": …}`` frame, OpenRouter)."""
    message = _error_message(error, str(error)[:200])
    _LOGGER.error("%s stream error: %s", _LABEL, message)
    raise BackendStreamError(f"{_LABEL} stream error: {message}")


def _base_url(data: Mapping[str, Any]) -> str:
    """Return the connection base URL with any trailing slash stripped.

    A trailing ``/`` double-slashes the path and 404s ``/v1/models`` /
    ``/v1/chat/completions``; the parent flow normalizes it, but strip defensively.
    """
    return str(data[CONF_BASE_URL]).rstrip("/")


def _accumulate_tool_call(tool_calls: dict[int, dict[str, str]], fragment: object) -> None:
    """Fold one streamed ``delta.tool_calls`` fragment into the by-index accumulator.

    ``id``/``function.name`` appear on the first fragment for an index;
    ``function.arguments`` streams as a string concatenated across fragments. Missing
    fields are treated as empty (``get(...) or ""``) so a fragment carrying only more
    argument text folds in cleanly. Absent ``index`` defaults to 0 (single-call servers).
    A non-object fragment raises (the entity guard maps it to the fallback).
    """
    call = json_object(fragment)
    if call is None:
        raise BackendStreamError(f"Malformed tool_calls fragment: {str(fragment)[:200]}")
    slot = tool_calls.setdefault(int(call.get("index", 0)), {"id": "", "name": "", "args": ""})
    slot["id"] += call.get("id") or ""
    if (function := json_object(call.get("function"))) is not None:
        slot["name"] += function.get("name") or ""
        slot["args"] += function.get("arguments") or ""


def _tool_input(call: dict[str, str]) -> llm.ToolInput | None:
    """Build a ``ToolInput`` from an accumulated call, or ``None`` if its arguments are bad.

    Small models sometimes stream truncated/invalid JSON arguments; dropping that one
    call (logged) keeps the rest of the turn alive instead of failing it wholesale.
    No streamed ``arguments`` (a no-arg call) means ``{}``.
    """
    args = loads_json_object(call["args"] or "{}")
    if args is None:
        _LOGGER.warning(
            "Skipping tool call %s (%s): arguments are not a JSON object: %s",
            call["id"],
            call["name"],
            call["args"][:200],
        )
        return None
    return llm.ToolInput(id=call["id"], tool_name=call["name"], tool_args=args)


def _convert_content(item: conversation.Content, images: Sequence[EncodedImage] = ()) -> dict[str, Any]:
    """Map one HA ChatLog content item to an OpenAI chat message.

    Assistant turns carry any ``tool_calls`` (LLMM-014) so a re-entered tool loop
    replays them; the closed ``Content`` union makes the final branch the tool-result
    case, serialized with ``default=str`` (a ``time``/``datetime`` tool result is not
    otherwise JSON-serializable). A user turn with ``images`` (AI Task attachments,
    already encoded) becomes a content-parts array with base64 data-URL ``image_url`` parts.
    """
    if isinstance(item, conversation.SystemContent):
        return {"role": "system", "content": item.content}
    if isinstance(item, conversation.UserContent):
        if not images:
            return {"role": "user", "content": item.content}
        parts: list[dict[str, Any]] = [{"type": "text", "text": item.content}]
        parts.extend(
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data}"}} for mime_type, data in images
        )
        return {"role": "user", "content": parts}
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
        "content": tool_result_json(item),
    }


class OpenAICompatAdapter(BackendAdapter):
    """Stateless-replay adapter for OpenAI-compatible ``/v1/chat/completions``."""

    backend_type: ClassVar[str] = BACKEND_OPENAI_COMPAT
    # Formats HA tool schemas + parses streamed tool_calls (LLMM-014), so the subentry
    # flow offers CONF_LLM_HASS_API and the entity runs the tool loop.
    supports_ha_tools: ClassVar[bool] = True
    # Replays chat_log (whose system prompt carries HA's date/time); never reads user_input.text.
    wants_time_prefix: ClassVar[bool] = False
    # Sends TurnContext.output_schema as response_format and images as content parts (LLMM-023).
    supports_ai_task: ClassVar[bool] = True
    # Connection capabilities, not per-turn state: learned from a refused request so later
    # turns skip the doomed attempt. Reassigned (never mutated), so a concurrent update is benign.
    _dropped_fields: frozenset[str] = frozenset()
    # Models that rejected max_tokens (OpenAI reasoning models): sent max_completion_tokens.
    _max_completion_models: frozenset[str] = frozenset()

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

        Maps 401/403 → :class:`BackendAuthError`; any other error status, transport/timeout
        failure, or non-object body → :class:`BackendConnectionError` (``probe_json``).
        """
        payload = await probe_json(
            async_get_clientsession(hass),
            f"{_base_url(data)}/v1/models",
            label="OpenAI-compatible backend",
            headers=bearer_headers(data.get(CONF_API_KEY)),
        )
        entries: Any = payload.get("data", [])
        models: list[str] = []
        for entry in entries:
            entry_id = (json_object(entry) or {}).get("id")
            if isinstance(entry_id, str):
                models.append(entry_id)
        return models

    async def _build_messages(self, chat_log: conversation.ChatLog, ctx: TurnContext) -> list[dict[str, Any]]:
        """Replay ``chat_log.content`` (with any image attachments) to trimmed OpenAI ``messages[]``."""
        items = replayable_content(chat_log.content)
        images = await async_load_images(self.hass, items)
        messages = [_convert_content(item, item_images) for item, item_images in zip(items, images, strict=True)]
        max_history = int(ctx.options.get(CONF_MAX_HISTORY, 0))
        return trim_history(messages, max_history)

    async def _build_body(self, chat_log: conversation.ChatLog, ctx: TurnContext) -> dict[str, Any]:
        """Assemble the chat/completions request body from history + options."""
        body: dict[str, Any] = {
            "messages": await self._build_messages(chat_log, ctx),
            "stream": True,
        }
        if (model := ctx.options.get(CONF_MODEL)) is not None:
            body["model"] = model
        for key, convert in _OPTION_KEYS:
            if (value := ctx.options.get(key)) is not None:
                body[key] = convert(value)
        if effort := ctx.options.get(CONF_REASONING_EFFORT):
            body["reasoning_effort"] = effort
        body["stream_options"] = _STREAM_OPTIONS
        # Per agent, like core openai: the system prompt + tools prefix is shared across
        # the agent's conversations, so they hit the same provider prompt cache.
        if ctx.subentry_id is not None:
            body["prompt_cache_key"] = ctx.subentry_id
        # HA tool schemas, only when an LLM API is configured for this agent.
        if chat_log.llm_api is not None and chat_log.llm_api.tools:
            body["tools"] = [
                format_function_tool(tool, chat_log.llm_api.custom_serializer) for tool in chat_log.llm_api.tools
            ]
        # AI Task structured output. strict=False: strict mode demands every property be
        # required and additionalProperties false, which HA schemas don't guarantee. A
        # server that rejects json_schema fails the request with its own error body (logged).
        if ctx.output_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": ctx.output_schema.name, "schema": ctx.output_schema.schema, "strict": False},
            }
        return body

    def _request(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Apply what the connection learned (dropped fields, max_completion_tokens) to ``body``."""
        request = {key: value for key, value in body.items() if key not in self._dropped_fields}
        if "max_tokens" in request and str(body.get("model") or "") in self._max_completion_models:
            request["max_completion_tokens"] = request.pop("max_tokens")
        return request

    def _learn(self, fields: frozenset[str], body: Mapping[str, Any]) -> None:
        """Remember refused fields so this and later requests leave them out."""
        _LOGGER.debug("%s refused %s; retrying without", _LABEL, ", ".join(sorted(fields)))
        self._dropped_fields |= fields & frozenset(_DROPPABLE_FIELDS)
        if "max_tokens" in fields:
            self._max_completion_models |= {str(body.get("model") or "")}

    @asynccontextmanager
    async def _open_stream(
        self, url: str, body: dict[str, Any], headers: dict[str, str], timeout: aiohttp.ClientTimeout
    ) -> AsyncGenerator[aiohttp.ClientResponse]:
        """POST the turn, re-POSTing without each field the server refuses by name."""
        # Bounded: a rejection only names fields present in the request, and _learn
        # removes (or renames) each of them, so every retry sends fewer refusable fields.
        while True:
            request = self._request(body)
            try:
                async with self._post_checked(
                    url,
                    label=_LABEL,
                    json=request,
                    headers=headers,
                    timeout=timeout,
                    on_error_status=functools.partial(_check_error_status, request),
                ) as response:
                    yield response
                return
            except _FieldsRejectedError as err:
                self._learn(err.fields, body)

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
        Reasoning deltas stream as ``{"thinking_content": …}``; the last ``usage`` seen
        is traced once as ``stats`` after the stream ends. An ``error`` object or
        ``finish_reason: "error"`` raises after any text already streamed (the entity
        guard appends the fallback); a ``length`` / ``content_filter`` finish is recorded.
        """
        headers = {"Accept": "text/event-stream", **bearer_headers(self.connection_data.get(CONF_API_KEY))}
        url = f"{_base_url(self.connection_data)}/v1/chat/completions"
        role_sent = False
        # index -> partial call; id/name arrive on the first fragment, arguments stream
        # as a string concatenated across fragments (chat/completions delta.tool_calls).
        tool_calls: dict[int, dict[str, str]] = {}
        usage: dict[str, Any] | None = None
        truncated: str | None = None
        async with self._open_stream(
            url, await self._build_body(chat_log, ctx), headers, build_client_timeout(ctx.options)
        ) as response:
            async for event in async_iter_sse(response.content.iter_any()):
                if event.data == _DONE_SENTINEL:
                    break
                # A non-object chunk/delta reads as empty; a non-conforming ``choices``
                # raises here and the entity's _guarded wrapper maps it to the fallback.
                chunk = json_object(json.loads(event.data)) or {}
                # vLLM sends a bare {"error": …} frame; OpenRouter puts it beside choices.
                if (error := chunk.get("error")) is not None:
                    _raise_stream_error(error)
                # include_usage puts usage on a terminal choices-less chunk; some servers
                # repeat it on every chunk, so keep the last one seen.
                if (chunk_usage := json_object(chunk.get("usage"))) is not None:
                    usage = chunk_usage
                choices: Any = chunk.get("choices")
                if not choices:
                    continue
                choice: dict[str, Any] = choices[0]
                delta = json_object(choice.get("delta")) or {}
                fragments: Any = delta.get("tool_calls")
                if fragments:
                    for fragment in fragments:
                        _accumulate_tool_call(tool_calls, fragment)
                # DeepSeek/vLLM/llama.cpp say reasoning_content, OpenRouter reasoning;
                # servers mid-rename send both with the same text, so take one.
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning and isinstance(reasoning, str):
                    if not role_sent:
                        yield {"role": "assistant"}
                        role_sent = True
                    yield {"thinking_content": reasoning}
                # A refusal is the model's answer (content stays null), so it is spoken.
                refusal = delta.get("refusal")
                content = refusal if refusal and isinstance(refusal, str) else delta.get("content")
                if content is not None:
                    if not role_sent:
                        yield {"role": "assistant"}
                        role_sent = True
                    yield {"content": content}
                finish_reason = choice.get("finish_reason")
                if finish_reason == "error":
                    raise BackendStreamError(f"{_LABEL} stream ended with finish_reason error")
                if finish_reason in _TRUNCATED:
                    truncated = finish_reason
        if truncated is not None:
            record_truncation(chat_log, _LABEL, truncated)
        if usage is not None:
            details = json_object(usage.get("prompt_tokens_details")) or {}
            reasoning_tokens = (json_object(usage.get("completion_tokens_details")) or {}).get("reasoning_tokens")
            trace_token_stats(
                chat_log,
                input_tokens=int(usage.get("prompt_tokens") or 0),
                cached_input_tokens=int(details.get("cached_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                reasoning_tokens=reasoning_tokens if isinstance(reasoning_tokens, int) else None,
            )
        # Flush accumulated tool calls once, after [DONE]/EOF (some servers omit the id
        # on later fragments and/or the finish_reason, so index-keyed accumulation +
        # a single terminal flush is the tolerant contract).
        tool_inputs = [tool_input for call in tool_calls.values() if (tool_input := _tool_input(call)) is not None]
        if tool_inputs:
            if not role_sent:
                yield {"role": "assistant"}
                role_sent = True
            yield {"tool_calls": tool_inputs}
