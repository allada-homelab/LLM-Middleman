"""n8n backend adapter (Chat Trigger / plain Webhook, dual streaming/blocking).

n8n is the one connector that silently degrades from a streamed NDJSON reply to a
single blocking JSON body when the workflow is not stream-enabled on **both** the
Chat Trigger and the AI Agent node. So the adapter must branch on the **actual
response** (content-type / first bytes), never on the ``streaming`` config toggle.

Streaming is NDJSON ``StructuredChunk`` lines
(``{"type":"begin"|"item"|"end"|"error","content":…}``, n8n 1.103.0) — NOT SSE; lines
are framed by the shared, line-capped :func:`._sse.async_iter_lines`. ``item.content`` is accumulated and streamed
delta-by-delta; **EOF (the stream closing) is the true done signal** — a run may emit
several ``begin``/``end`` cycles, so ``end`` is never treated as terminal.

Stateful: the entity's ``ctx.memory_key`` is sent as the configured session field
(default ``sessionId``), which n8n's memory nodes partition on.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Mapping
from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

import aiohttp
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant

from ..const import (  # noqa: TID252 — sibling package const module
    BACKEND_N8N,
    CONF_AUTH_TYPE,
    CONF_HEADER_NAME,
    CONF_HEADER_VALUE,
    CONF_INPUT_FIELD,
    CONF_OUTPUT_FIELD,
    CONF_PASSWORD,
    CONF_SESSION_FIELD,
    CONF_TARGET_TYPE,
    CONF_USERNAME,
    CONF_WEBHOOK_URL,
    N8N_AUTH_BASIC,
    N8N_AUTH_HEADER,
    N8N_AUTH_NONE,
    N8N_DEFAULT_INPUT_FIELD,
    N8N_DEFAULT_OUTPUT_FIELD,
    N8N_DEFAULT_SESSION_FIELD,
    N8N_DEFAULT_TIMEOUT,
    N8N_OUTPUT_FIELD_FALLBACK,
    N8N_TARGET_WEBHOOK,
)
from ._sse import MAX_LINE_BYTES, async_iter_lines
from .base import (
    BackendAdapter,
    BackendConnectionError,
    BackendStreamError,
    DeltaStream,
    TurnContext,
    build_client_timeout,
    loads_json_object,
    rendered_system_prompt,
)

_LOGGER = logging.getLogger(__name__)

# The Chat Trigger node distinguishes turns by this action; plain Webhook omits it.
_ACTION_SEND_MESSAGE = "sendMessage"
# StructuredChunk envelope types (n8n 1.103.0). ``begin``/``end`` are segment markers,
# not stream terminators — a single run may emit several begin/end cycles.
_STRUCTURED_CHUNK_TYPES = frozenset({"begin", "item", "end", "error"})
# A blocking (non-streamed) reply is read whole before parsing, so it is capped: 1 MiB is
# far beyond any spoken reply, and an over-cap body fails the turn instead of growing memory.
_MAX_BLOCKING_BYTES = 1 << 20

_Mode = Literal["html", "ndjson", "blocking"]


def _sniff_mode(sample: bytes, content_type: str) -> _Mode:
    """Classify the real response from its first line and content-type.

    ``sample`` is the first newline-terminated line (or the whole body at EOF). An
    HTML body (proxy/gateway timeout) is detected first; a ``StructuredChunk`` first
    line — or an NDJSON content-type — means streaming; anything else is a single
    blocking JSON body. The ``streaming`` config toggle is deliberately ignored.
    """
    stripped = sample.lstrip()
    if "html" in content_type or stripped[:1] == b"<":
        return "html"
    obj = loads_json_object(sample)
    if obj is not None and obj.get("type") in _STRUCTURED_CHUNK_TYPES:
        return "ndjson"
    if any(hint in content_type for hint in ("ndjson", "json-lines", "jsonl")):
        return "ndjson"
    return "blocking"


async def _next_chunk(stream: AsyncIterator[bytes]) -> bytes | None:
    """Pull the next byte chunk, or ``None`` at EOF."""
    try:
        return await anext(stream)
    except StopAsyncIteration:
        return None


async def _prepend(head: bytes, rest: AsyncIterator[bytes]) -> AsyncGenerator[bytes]:
    """Re-attach the bytes consumed while sniffing to the front of the remaining stream."""
    if head:
        yield head
    async for chunk in rest:
        yield chunk


class N8nAdapter(BackendAdapter):
    """n8n preset: stateful, dual streaming/blocking, no HA tools (n8n owns its agent)."""

    backend_type: ClassVar[str] = BACKEND_N8N
    supports_ha_tools: ClassVar[bool] = False
    supports_memory_scope: ClassVar[bool] = True
    # Forwards the rendered system prompt (with HA's date/time) as ``systemPrompt``.
    wants_time_prefix: ClassVar[bool] = False

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        """Validate the webhook URL shape only — no live probe.

        The webhook is opaque and a probing ``POST`` would fire the live workflow, so
        there is no safe network check at config time (E2E is the real validation —
        LLMM-018). We reject only a missing or non-absolute-http(s) URL.
        """
        url = data.get(CONF_WEBHOOK_URL)
        if not isinstance(url, str) or not url:
            raise BackendConnectionError("n8n webhook URL is required")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BackendConnectionError("n8n webhook URL must be an absolute http(s) URL")

    async def stream_turn(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        ctx: TurnContext,
    ) -> DeltaStream:
        """POST the turn to the webhook and stream the reply back as HA deltas.

        n8n is stateful and owns its own history, so only the new turn text (plus the
        rendered system prompt from ``chat_log``) is sent, keyed on ``ctx.memory_key``.
        """
        url = self.connection_data[CONF_WEBHOOK_URL]
        body = self._request_body(user_input, ctx, rendered_system_prompt(chat_log))
        headers = self._request_headers()
        # Log the auth *type* only — never the credential value.
        _LOGGER.debug("n8n POST %s (auth=%s)", url, self._auth_type())
        try:
            async with self._post_checked(
                url,
                label="n8n webhook",
                data=json.dumps(body, default=str),
                headers=headers,
                timeout=build_client_timeout(ctx.options, N8N_DEFAULT_TIMEOUT),
            ) as response:
                async for delta in self._iter_deltas(response, ctx):
                    yield delta
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BackendStreamError(f"n8n request failed: {err}") from err

    def _auth_type(self) -> str:
        return self.connection_data.get(CONF_AUTH_TYPE, N8N_AUTH_NONE)

    def _request_body(
        self, user_input: conversation.ConversationInput, ctx: TurnContext, system_prompt: str | None
    ) -> dict[str, Any]:
        """Build the request body: session key + turn text (+ optional action/system prompt)."""
        session_field = ctx.options.get(CONF_SESSION_FIELD, N8N_DEFAULT_SESSION_FIELD)
        input_field = ctx.options.get(CONF_INPUT_FIELD, N8N_DEFAULT_INPUT_FIELD)
        body: dict[str, Any] = {session_field: ctx.memory_key, input_field: user_input.text}
        # Only the Chat Trigger node uses ``action``; plain Webhook + Respond-to-Webhook omits it.
        if self.connection_data.get(CONF_TARGET_TYPE) != N8N_TARGET_WEBHOOK:
            body["action"] = _ACTION_SEND_MESSAGE
        if system_prompt:
            body["systemPrompt"] = system_prompt
        return body

    def _request_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json-lines, application/json"}
        auth_type = self._auth_type()
        if auth_type == N8N_AUTH_HEADER:
            name = self.connection_data.get(CONF_HEADER_NAME)
            value = self.connection_data.get(CONF_HEADER_VALUE)
            if name and value:
                headers[name] = value
        elif auth_type == N8N_AUTH_BASIC:
            username = self.connection_data.get(CONF_USERNAME, "")
            password = self.connection_data.get(CONF_PASSWORD, "")
            # aiohttp.BasicAuth is deprecated (removed in 4.0); encode_basic_auth
            # yields the identical "Basic <base64>" header value.
            headers["Authorization"] = aiohttp.encode_basic_auth(username, password)
        return headers

    async def _iter_deltas(self, response: aiohttp.ClientResponse, ctx: TurnContext) -> DeltaStream:
        """Sniff the real response mode, then stream NDJSON deltas or parse a blocking body."""
        content_type = response.headers.get("Content-Type", "").lower()
        stream = response.content.iter_any()
        buffer = bytearray()
        exhausted = False

        # Phase 1: buffer up to the first line boundary (or EOF, or the line cap) to sniff
        # the real mode.
        while b"\n" not in buffer and len(buffer) <= MAX_LINE_BYTES:
            chunk = await _next_chunk(stream)
            if chunk is None:
                exhausted = True
                break
            buffer += chunk

        newline_idx = buffer.find(b"\n")
        sample = bytes(buffer) if newline_idx == -1 else bytes(buffer[:newline_idx])
        mode = _sniff_mode(sample, content_type)

        if mode == "html":
            raise BackendStreamError("n8n returned a non-JSON (HTML) body — likely a proxy/gateway timeout")

        if mode == "blocking":
            if not exhausted:
                async for chunk in stream:
                    buffer += chunk
                    if len(buffer) > _MAX_BLOCKING_BYTES:
                        break
            if len(buffer) > _MAX_BLOCKING_BYTES:
                raise BackendStreamError(f"n8n blocking reply exceeded {_MAX_BLOCKING_BYTES} bytes")
            for delta in self._blocking_deltas(bytes(buffer), ctx):
                yield delta
            return

        # NDJSON streaming path: emit item content as it arrives; EOF is the true done
        # signal, and a final line with no trailing newline still counts.
        started = False
        async for line in async_iter_lines(_prepend(bytes(buffer), stream)):
            for content in _line_contents(line):
                if not started:
                    started = True
                    yield {"role": "assistant"}
                yield {"content": content}

        if not started:
            raise BackendStreamError("n8n stream closed without producing any content")

    def _blocking_deltas(self, raw: bytes, ctx: TurnContext) -> Iterator[conversation.AssistantContentDeltaDict]:
        """Parse a single blocking JSON body via the ``output_field`` → ``text`` fallback chain."""
        obj = loads_json_object(raw)
        if obj is None:
            raise BackendStreamError("n8n blocking reply was not a JSON object")
        output_field = ctx.options.get(CONF_OUTPUT_FIELD, N8N_DEFAULT_OUTPUT_FIELD)
        reply = obj.get(output_field)
        if reply is None:
            reply = obj.get(N8N_OUTPUT_FIELD_FALLBACK)
        if reply is None:
            # Never json.dumps the whole object and speak it — surface the misconfig instead.
            raise BackendStreamError(f"n8n reply missing '{output_field}'/'{N8N_OUTPUT_FIELD_FALLBACK}' field")
        if obj.get("continueConversation"):
            ctx.continue_conversation = True
        yield {"role": "assistant"}
        yield {"content": reply if isinstance(reply, str) else str(reply)}


def _line_contents(line: bytes) -> Iterator[str]:
    """Yield the ``item`` content of one NDJSON ``StructuredChunk`` line.

    Blank, malformed, and non-object lines are skipped (logged, tolerant). ``begin``,
    ``end``, and unknown types yield nothing (``end`` is not terminal). An ``error``
    chunk raises :class:`BackendStreamError`. Content whitespace is never stripped.
    """
    if not line.strip():
        return
    obj = loads_json_object(line)
    if obj is None:
        _LOGGER.warning("n8n: skipping malformed NDJSON line")
        return
    chunk_type = obj.get("type")
    if chunk_type == "error":
        raise BackendStreamError(f"n8n error chunk: {obj.get('content')!r}")
    if chunk_type == "item":
        content = obj.get("content")
        if isinstance(content, str) and content:
            yield content
