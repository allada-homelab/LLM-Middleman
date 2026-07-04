"""n8n backend adapter (Chat Trigger / plain Webhook, dual streaming/blocking).

n8n is the one connector that silently degrades from a streamed NDJSON reply to a
single blocking JSON body when the workflow is not stream-enabled on **both** the
Chat Trigger and the AI Agent node. So the adapter must branch on the **actual
response** (content-type / first bytes), never on the ``streaming`` config toggle.

Streaming is NDJSON ``StructuredChunk`` lines
(``{"type":"begin"|"item"|"end"|"error","content":…}``, n8n 1.103.0) — NOT SSE, so
this module does not use ``_sse.py``. ``item.content`` is accumulated and streamed
delta-by-delta; **EOF (the stream closing) is the true done signal** — a run may emit
several ``begin``/``end`` cycles, so ``end`` is never treated as terminal.

Stateful: the entity's ``ctx.memory_key`` is sent as the configured session field
(default ``sessionId``), which n8n's memory nodes partition on.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any, ClassVar, Literal, cast
from urllib.parse import urlparse

import aiohttp
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant

from ..const import (  # noqa: TID252 — sibling package const module
    BACKEND_N8N,
    CONF_INPUT_FIELD,
    CONF_N8N_AUTH_TYPE,
    CONF_N8N_HEADER_NAME,
    CONF_N8N_HEADER_VALUE,
    CONF_N8N_PASSWORD,
    CONF_N8N_USERNAME,
    CONF_OUTPUT_FIELD,
    CONF_SESSION_FIELD,
    CONF_SYSTEM_PROMPT,
    CONF_TARGET_TYPE,
    CONF_TIMEOUT,
    CONF_WEBHOOK_URL,
    N8N_AUTH_BASIC,
    N8N_AUTH_HEADER,
    N8N_AUTH_NONE,
    N8N_DEFAULT_INPUT_FIELD,
    N8N_DEFAULT_OUTPUT_FIELD,
    N8N_DEFAULT_SESSION_FIELD,
    N8N_DEFAULT_TIMEOUT,
    N8N_OUTPUT_FIELD_FALLBACK,
    TARGET_PLAIN_WEBHOOK,
)
from .base import BackendAdapter, BackendConnectionError, BackendStreamError, DeltaStream, TurnContext

_LOGGER = logging.getLogger(__name__)

# The Chat Trigger node distinguishes turns by this action; plain Webhook omits it.
_ACTION_SEND_MESSAGE = "sendMessage"
# StructuredChunk envelope types (n8n 1.103.0). ``begin``/``end`` are segment markers,
# not stream terminators — a single run may emit several begin/end cycles.
_STRUCTURED_CHUNK_TYPES = frozenset({"begin", "item", "end", "error"})

_Mode = Literal["html", "ndjson", "blocking"]


def _loads_object(raw: bytes) -> dict[str, Any] | None:
    """Parse ``raw`` as a JSON object, returning ``None`` on any non-object/parse error."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return None


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
    obj = _loads_object(sample)
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


class N8nAdapter(BackendAdapter):
    """n8n preset: stateful, dual streaming/blocking, no HA tools (n8n owns its agent)."""

    backend_type: ClassVar[str] = BACKEND_N8N
    supports_ha_tools: ClassVar[bool] = False
    supports_memory_scope: ClassVar[bool] = True

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

        ``chat_log`` is unused: n8n is stateful and owns its own history, so only the
        new turn text is sent, keyed on ``ctx.memory_key``.
        """
        url = self.connection_data[CONF_WEBHOOK_URL]
        body = self._request_body(user_input, ctx)
        headers = self._request_headers()
        auth = self._request_auth()
        timeout = ctx.options.get(CONF_TIMEOUT, N8N_DEFAULT_TIMEOUT)
        # Log the auth *type* only — never the credential value.
        _LOGGER.debug("n8n POST %s (auth=%s)", url, self._auth_type())
        try:
            async with self.session.post(
                url,
                data=json.dumps(body, default=str),
                headers=headers,
                auth=auth,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                async for delta in self._iter_deltas(response, ctx):
                    yield delta
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BackendStreamError(f"n8n request failed: {err}") from err

    def _auth_type(self) -> str:
        return self.connection_data.get(CONF_N8N_AUTH_TYPE, N8N_AUTH_NONE)

    def _request_body(self, user_input: conversation.ConversationInput, ctx: TurnContext) -> dict[str, Any]:
        """Build the request body: session key + turn text (+ optional action/system prompt)."""
        session_field = ctx.options.get(CONF_SESSION_FIELD, N8N_DEFAULT_SESSION_FIELD)
        input_field = ctx.options.get(CONF_INPUT_FIELD, N8N_DEFAULT_INPUT_FIELD)
        body: dict[str, Any] = {session_field: ctx.memory_key, input_field: user_input.text}
        # Only the Chat Trigger node uses ``action``; plain Webhook + Respond-to-Webhook omits it.
        if self.connection_data.get(CONF_TARGET_TYPE) != TARGET_PLAIN_WEBHOOK:
            body["action"] = _ACTION_SEND_MESSAGE
        if system_prompt := ctx.options.get(CONF_SYSTEM_PROMPT):
            body["systemPrompt"] = system_prompt
        return body

    def _request_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json-lines, application/json"}
        if self._auth_type() == N8N_AUTH_HEADER:
            name = self.connection_data.get(CONF_N8N_HEADER_NAME)
            value = self.connection_data.get(CONF_N8N_HEADER_VALUE)
            if name and value:
                headers[name] = value
        return headers

    def _request_auth(self) -> aiohttp.BasicAuth | None:
        if self._auth_type() == N8N_AUTH_BASIC:
            username = self.connection_data.get(CONF_N8N_USERNAME, "")
            password = self.connection_data.get(CONF_N8N_PASSWORD, "")
            return aiohttp.BasicAuth(username, password)
        return None

    async def _iter_deltas(self, response: aiohttp.ClientResponse, ctx: TurnContext) -> DeltaStream:
        """Sniff the real response mode, then stream NDJSON deltas or parse a blocking body."""
        content_type = response.headers.get("Content-Type", "").lower()
        stream = response.content.iter_any()
        buffer = bytearray()
        exhausted = False

        # Phase 1: buffer up to the first line boundary (or EOF) to sniff the real mode.
        while b"\n" not in buffer:
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
            for delta in self._blocking_deltas(bytes(buffer), ctx):
                yield delta
            return

        # NDJSON streaming path: emit item content as it arrives; EOF is the true done signal.
        started = False
        while True:
            idx = buffer.find(b"\n")
            while idx != -1:
                line = bytes(buffer[:idx])
                del buffer[: idx + 1]
                for content in _line_contents(line):
                    if not started:
                        started = True
                        yield {"role": "assistant"}
                    yield {"content": content}
                idx = buffer.find(b"\n")
            if exhausted:
                break
            chunk = await _next_chunk(stream)
            if chunk is None:
                exhausted = True
                continue
            buffer += chunk

        # A final line with no trailing newline still counts.
        if buffer.strip():
            for content in _line_contents(bytes(buffer)):
                if not started:
                    started = True
                    yield {"role": "assistant"}
                yield {"content": content}

        if not started:
            raise BackendStreamError("n8n stream closed without producing any content")

    def _blocking_deltas(self, raw: bytes, ctx: TurnContext) -> Iterator[conversation.AssistantContentDeltaDict]:
        """Parse a single blocking JSON body via the ``output_field`` → ``text`` fallback chain."""
        obj = _loads_object(raw)
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
    obj = _loads_object(line)
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
