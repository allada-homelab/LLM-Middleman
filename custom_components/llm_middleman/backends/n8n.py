"""n8n backend adapter (Chat Trigger / plain Webhook, dual streaming/blocking).

n8n is the one connector that silently degrades from a streamed NDJSON reply to a
single blocking JSON body when the workflow is not stream-enabled on **both** the
Chat Trigger and the AI Agent node. So the adapter must branch on the **actual
response** (content-type / first bytes), never on the ``streaming`` config toggle.

Streaming is NDJSON ``StructuredChunk`` lines
(``{"type":"begin"|"item"|"end"|"error","content":…}``, n8n 1.103.0) — NOT SSE; lines
are framed by the shared, line-capped :func:`._sse.async_iter_lines`. ``item.content`` is accumulated and streamed
delta-by-delta; **EOF (the stream closing) is the true done signal** — a run may emit
several ``begin``/``end`` cycles, so ``end`` is never treated as terminal; consecutive
segments are separated by a newline (n8n's own chat client shows one message per segment).

Non-streamed replies come in the shapes n8n actually sends (LLMM-025, captured on 2.44.6):
a JSON object, a JSON array ("All Entries" / "All Incoming Items" — every item's reply is
joined), or a Respond-to-Webhook "Text" body (served as ``text/html``). A streamed item whose
content is a JSON string (Respond "JSON" / "First Incoming Item" in streaming mode) is
unwrapped the same way. AI Agent ``intermediateSteps`` in a blocking reply are surfaced as
external tool calls.

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
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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
    DEFAULT_TIMEOUT,
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
from ._compat import AssistantDelta, external_tool_call_delta, external_tool_result_delta
from ._sse import MAX_LINE_BYTES, async_iter_lines
from .base import (
    BackendAdapter,
    BackendAuthError,
    BackendConnectionError,
    BackendStreamError,
    DeltaStream,
    TurnContext,
    build_client_timeout,
    json_list,
    json_object,
    loads_json_object,
    rendered_system_prompt,
)

_LOGGER = logging.getLogger(__name__)

# The Chat Trigger node distinguishes turns by this action; plain Webhook omits it.
_ACTION_SEND_MESSAGE = "sendMessage"
# StructuredChunk envelope types (n8n 1.103.0). ``begin``/``end`` are segment markers,
# not stream terminators — a single run may emit several begin/end cycles. n8n >= 2.4x
# also heartbeats ``keepalive`` every 30 s, which may be the first line and carries no text.
_STRUCTURED_CHUNK_TYPES = frozenset({"begin", "item", "end", "error", "keepalive"})
# A blocking (non-streamed) reply is read whole before parsing, so it is capped: 1 MiB is
# far beyond any spoken reply, and an over-cap body fails the turn instead of growing memory.
_MAX_BLOCKING_BYTES = 1 << 20
# Joins streamed segments and array items: each is a separate utterance (one chat message
# per segment in n8n's own client); a newline keeps sentences apart for TTS and the chat log.
_SEGMENT_SEPARATOR = "\n"
# Only a real HTML *document* is an error page (a login portal / proxy page answering 200):
# Respond-to-Webhook "Text" is also served as text/html, but its body is the bare reply.
_HTML_DOCUMENT_PREFIXES = (b"<!doctype", b"<html")
# n8n's test endpoint answers once per "Execute workflow" click — never a persistent agent URL.
_TEST_WEBHOOK_SEGMENT = "/webhook-test/"
# n8n's WebhookNotFoundError text for an unregistered path (inactive workflow / wrong URL).
_NOT_REGISTERED_MARKER = "is not registered"

_Mode = Literal["html", "ndjson", "text", "blocking"]


class N8nWebhookError(BackendConnectionError):
    """The webhook URL is reachable but cannot serve turns; ``error_key`` names the form error."""

    def __init__(self, error_key: str, message: str) -> None:
        """Store the config-flow error key alongside the log message."""
        super().__init__(message)
        self.error_key = error_key


def _sniff_mode(sample: bytes, content_type: str) -> _Mode:
    """Classify the real response from its first line and content-type.

    ``sample`` is the first newline-terminated line (or the whole body at EOF). An HTML
    document (a 200 portal/proxy page; error statuses never get here) is detected first; a
    ``StructuredChunk`` first line — or an NDJSON content-type — means streaming; any other
    ``text/*`` body is a plain-text reply (Respond-to-Webhook "Text" is served as
    ``text/html``); anything else is a single blocking JSON body. The ``streaming`` config
    toggle is deliberately ignored.
    """
    if sample.lstrip()[:16].lower().startswith(_HTML_DOCUMENT_PREFIXES):
        return "html"
    obj = loads_json_object(sample)
    if obj is not None and obj.get("type") in _STRUCTURED_CHUNK_TYPES:
        return "ndjson"
    if any(hint in content_type for hint in ("ndjson", "json-lines", "jsonl")):
        return "ndjson"
    if content_type.startswith("text/"):
        return "text"
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
        """Check the URL shape, reject test URLs, then probe registration without a run.

        ``POST`` is a turn and ``GET`` starts a run on a Chat Trigger (or on a Webhook that
        also accepts GET), so the probe is ``OPTIONS`` with an ``Origin`` header: n8n resolves
        the path's registered methods into ``Access-Control-Allow-Methods`` and returns before
        executing anything (verified on 2.44.6 for both targets). An unregistered path
        (inactive workflow, wrong URL) lists no ``POST`` — or, on other versions, 404s with
        n8n's "is not registered" text. ``Access-Control-Request-Method`` is never sent: n8n
        answers 500 for an unknown path with it. Any other answer (a reverse proxy handling
        ``OPTIONS`` itself) is accepted — the turn surfaces a real failure.
        """
        url = data.get(CONF_WEBHOOK_URL)
        if not isinstance(url, str) or not url:
            raise BackendConnectionError("n8n webhook URL is required")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BackendConnectionError("n8n webhook URL must be an absolute http(s) URL")
        if _TEST_WEBHOOK_SEGMENT in parsed.path:
            raise N8nWebhookError("webhook_test_url", "n8n test URLs answer only once; use the production URL")

        session = async_get_clientsession(hass)
        # The configured auth rides along for proxies that guard OPTIONS with the same credential.
        headers = {**cls(hass, session, data)._request_headers(), "Origin": f"{parsed.scheme}://{parsed.netloc}"}
        try:
            async with session.options(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
            ) as response:
                status = response.status
                allowed = response.headers.get("Access-Control-Allow-Methods")
                body = await response.text() if status == 404 else ""
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BackendConnectionError(f"Cannot reach the n8n webhook: {err}") from err
        if status in (401, 403):
            raise BackendAuthError(f"n8n webhook rejected credentials (HTTP {status})")
        lists_no_post = allowed is not None and "POST" not in {m.strip().upper() for m in allowed.split(",")}
        if (status == 404 and _NOT_REGISTERED_MARKER in body) or (200 <= status < 300 and lists_no_post):
            raise N8nWebhookError("webhook_not_registered", "n8n webhook is not registered (is the workflow active?)")

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
            raise BackendStreamError("n8n returned an HTML document — likely a proxy/login page, not the workflow")

        if mode in ("blocking", "text"):
            if not exhausted:
                async for chunk in stream:
                    buffer += chunk
                    if len(buffer) > _MAX_BLOCKING_BYTES:
                        break
            if len(buffer) > _MAX_BLOCKING_BYTES:
                raise BackendStreamError(f"n8n blocking reply exceeded {_MAX_BLOCKING_BYTES} bytes")
            deltas = _text_deltas(bytes(buffer)) if mode == "text" else _blocking_deltas(bytes(buffer), ctx)
            for delta in deltas:
                yield delta
            return

        # NDJSON streaming path: emit item content as it arrives; EOF is the true done
        # signal, and a final line with no trailing newline still counts.
        started = False
        separate = False  # an ``end`` closed a segment that already spoke: separate the next one
        async for line in async_iter_lines(_prepend(bytes(buffer), stream)):
            chunk = _structured_chunk(line)
            if chunk is None:
                continue
            chunk_type = chunk.get("type")
            if chunk_type == "error":
                raise BackendStreamError(_error_chunk_message(chunk))
            if chunk_type == "end":
                separate = started
                continue
            content = chunk.get("content")
            if chunk_type != "item" or not isinstance(content, str) or not content:
                continue
            content = _unwrap_json_content(content, ctx)
            if not started:
                started = True
                yield {"role": "assistant"}
            elif separate:
                content = _SEGMENT_SEPARATOR + content
            separate = False
            yield {"content": content}

        if not started:
            raise BackendStreamError("n8n stream closed without producing any content")


def _text_deltas(raw: bytes) -> Iterator[AssistantDelta]:
    """Speak a plain-text body (Respond-to-Webhook "Text") as the reply."""
    text = raw.decode(errors="replace")
    if not text.strip():
        raise BackendStreamError("n8n text reply was empty")
    yield {"role": "assistant"}
    yield {"content": text}


def _blocking_deltas(raw: bytes, ctx: TurnContext) -> Iterator[AssistantDelta]:
    """Parse a blocking JSON body (object or array): tool steps first, then the reply."""
    try:
        value: object = json.loads(raw)
    except ValueError as err:
        raise BackendStreamError("n8n blocking reply was not JSON") from err
    reply = _reply_from_json(value, ctx)
    if reply is None:
        # Never json.dumps the whole body and speak it — surface the misconfig instead.
        output_field = ctx.options.get(CONF_OUTPUT_FIELD, N8N_DEFAULT_OUTPUT_FIELD)
        raise BackendStreamError(f"n8n reply missing '{output_field}'/'{N8N_OUTPUT_FIELD_FALLBACK}' field")
    # Tool deltas precede the reply block, so the stream never ends on a tool result
    # (which would re-enter the entity's tool loop and re-send the turn).
    yield from _intermediate_step_deltas(_json_items(value))
    yield {"role": "assistant"}
    yield {"content": reply}


def _json_items(value: object) -> list[dict[str, Any]]:
    """The reply objects of a JSON body: the object itself, or an array's object items."""
    items = json_list(value)
    if items is None:
        obj = json_object(value)
        return [] if obj is None else [obj]
    return [obj for obj in map(json_object, items) if obj is not None]


def _reply_from_json(value: object, ctx: TurnContext) -> str | None:
    """Extract the reply via ``output_field`` → ``text``; an array's replies are joined in order.

    n8n's own chat client has no array handling (it stringifies the body), so joining every
    item's reply keeps all the text the workflow produced instead of dropping later items.
    ``continueConversation`` on a contributing item requests follow-up listening.
    """
    output_field = ctx.options.get(CONF_OUTPUT_FIELD, N8N_DEFAULT_OUTPUT_FIELD)
    replies: list[str] = []
    for obj in _json_items(value):
        reply = obj.get(output_field)
        if reply is None:
            reply = obj.get(N8N_OUTPUT_FIELD_FALLBACK)
        if reply is None:
            continue
        if obj.get("continueConversation"):
            ctx.continue_conversation = True
        replies.append(reply if isinstance(reply, str) else str(reply))
    return _SEGMENT_SEPARATOR.join(replies) if replies else None


def _unwrap_json_content(content: str, ctx: TurnContext) -> str:
    """Unwrap a streamed item that is a whole JSON reply (Respond "JSON" in streaming mode).

    Only a complete JSON object/array carrying the reply field is unwrapped; LLM token
    chunks and any other JSON are spoken verbatim.
    """
    if content.lstrip()[:1] not in ("{", "["):
        return content
    try:
        value: object = json.loads(content)
    except ValueError:
        return content
    reply = _reply_from_json(value, ctx)
    return content if reply is None else reply


def _intermediate_step_deltas(items: list[dict[str, Any]]) -> Iterator[AssistantDelta]:
    """Surface AI Agent ``intermediateSteps`` (Return Intermediate Steps) as external tool calls.

    Shape per n8n 2.44.6 ``buildSteps``: ``{action: {tool, toolInput, toolCallId, …},
    observation}`` where ``observation`` is a JSON string — the tool's items, or
    ``{"error": …}`` on failure. Malformed steps are skipped.
    """
    count = 0
    for item in items:
        for raw_step in json_list(item.get("intermediateSteps")) or []:
            step = json_object(raw_step) or {}
            action = json_object(step.get("action")) or {}
            name = action.get("tool")
            if not isinstance(name, str) or not name:
                continue
            count += 1
            call_id = action.get("toolCallId")
            if not isinstance(call_id, str) or not call_id:
                call_id = f"n8n-step-{count}"
            result = _decode_observation(step.get("observation"))
            error = (json_object(result) or {}).get("error") is not None
            yield external_tool_call_delta(call_id, name, json_object(action.get("toolInput")) or {})
            yield external_tool_result_delta(call_id, name, result, error=error)


def _decode_observation(observation: object) -> object:
    """Decode a step's JSON-string observation; a non-JSON string is kept as text."""
    if not isinstance(observation, str):
        return observation
    try:
        return json.loads(observation)
    except ValueError:
        return observation


def _structured_chunk(line: bytes) -> dict[str, Any] | None:
    """Parse one NDJSON ``StructuredChunk`` line; blank and malformed lines are skipped (logged)."""
    if not line.strip():
        return None
    obj = loads_json_object(line)
    if obj is None:
        _LOGGER.warning("n8n: skipping malformed NDJSON line")
    return obj


def _error_chunk_message(chunk: Mapping[str, Any]) -> str:
    """Describe an ``error`` chunk; n8n 2.44.6 sends ``content: null``, so name the node instead."""
    content = chunk.get("content")
    node = (json_object(chunk.get("metadata")) or {}).get("nodeName")
    where = f" in node {node!r}" if isinstance(node, str) and node else ""
    detail = content if isinstance(content, str) and content else "no detail sent; see the execution in n8n"
    return f"n8n workflow error{where}: {detail}"
