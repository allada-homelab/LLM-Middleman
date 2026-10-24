"""Dify backend adapter (LLMM-020, tier-1 hardening LLMM-021).

First-class preset for Dify (dify.ai / self-hosted ``langgenius/dify``). Dify exposes
one chat API for its Chatbot, Legacy Agent, Agent, and Chatflow app types:
``POST {base}/chat-messages`` with ``response_mode: "streaming"`` (blocking mode is
**not** supported for Agent apps, so this adapter is streaming-only). Replies stream as
WHATWG SSE (framed by :mod:`._sse`), but every event except ``ping`` is a bare
``data: {json}`` frame with the event name INSIDE the JSON (``{"event": "message", ...}``)
and no ``event:`` line — so dispatch is on ``payload["event"]``, falling back to the SSE
``event:`` field only when the JSON has no ``event`` key (a proxy that re-frames).
``ping`` arrives as ``event: ping`` with no data, which the framer never dispatches.

Per the Service API spec's "Events by app type" (``POST /chat-messages`` →
``text/event-stream``): ``agent_message`` carries answer deltas for (Legacy) Agent apps,
``message`` for Chatbot/Chatflow. A new-style Agent app closes its ``agent_message``
chunks with one ``message`` carrying the COMPLETE answer, so a ``message`` after any
``agent_message`` in the same stream is dropped (not re-spoken). Terminal states:

* ``message_end`` — success; its ``metadata.usage`` is recorded in the conversation trace.
* ``error`` (HTTP still 200; a failed Chatflow sends ``workflow_finished`` ``failed`` then
  ``error``) → :class:`BackendStreamError`.
* ``workflow_paused`` — a Chatflow reached a Human Input node; the stream ends there and
  the run resumes only through Dify's human-input form API, which a voice turn cannot
  drive. A short :data:`PAUSED_NOTICE` is spoken (not the error fallback — nothing
  failed) and the pause is recorded in the trace; the paused run is NOT stopped.
* EOF with none of the above (dropped connection/proxy cut) → :class:`BackendStreamError`,
  so a truncated reply is never presented as complete.

``message_replace`` (output moderation) replaces the answer: spoken only when nothing has
been spoken yet, otherwise recorded in the trace and logged (streamed TTS cannot be
un-spoken); further answer chunks are dropped either way. Chatflow ``reasoning_chunk``
deltas become ``thinking_content`` (shown, never spoken). Tool activity — Agent
``agent_thought`` tool/tool_input/observation and Chatflow ``tool`` nodes
(``node_started``/``node_finished``) — is recorded as external tool calls + results via
``_compat`` (shown in the Assist chat, never spoken, never executed by HA). A run that used
tools must still end with spoken text, else it raises so the entity appends its fallback
(ending on a tool result would re-enter the entity's tool loop and re-send the turn).
``message_file``/``tts_*``/``workflow_*``/other ``node_*``/… are not spoken.

Dify owns conversation memory server-side: the first turn returns a ``conversation_id``
on every event, and echoing it back continues the conversation → stateful
(``supports_memory_scope = True``). The server id is only known *mid-stream* (the first
event carrying it — for a Chatflow that is ``workflow_started``), so the
``session_key -> conversation_id`` map is persisted after capture — in-memory for
``conversation`` scope, via ``helpers.storage.Store`` for ``device``/``agent`` scope. A
stale/deleted id (pre-stream 404 ``not_found``) or an ended one (400
``conversation_completed``) is dropped and the turn retried once without an id. A
best-effort ``POST /chat-messages/{task_id}/stop`` (same ``user``, which the spec requires
for the stop to take effect) fires whenever the turn ends abnormally after a ``task_id``
was seen: cancellation, idle timeout, or any other error.

Per-agent options (LLMM-021): ``CONF_DIFY_INPUTS`` (static ``inputs`` mapping, for apps
whose ``user_input_form`` declares required variables), ``CONF_DIFY_HA_CONTEXT`` (fill the
``ha_*`` variables of :data:`HA_CONTEXT_INPUTS` from the turn — only those the app declares
in ``GET /parameters``, cached per entry), and ``CONF_DIFY_USER_FROM_HA`` (send the HA user
id as the Dify end-user instead of the shared ``home-assistant``).

Tools live inside the Dify app; the API has no client-tool passthrough
(``supports_ha_tools = False``). The per-agent system prompt is likewise owned by the
Dify app, so ``CONF_PROMPT`` is not forwarded. API reference (OpenAPI):
https://docs.dify.ai/en/api-reference/openapi_service.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from custom_components.llm_middleman.const import (
    BACKEND_DIFY,
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_DIFY_HA_CONTEXT,
    CONF_DIFY_INPUTS,
    CONF_DIFY_USER_FROM_HA,
    CONF_MEMORY_SCOPE,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MEMORY_SCOPE_AGENT,
    MEMORY_SCOPE_CONVERSATION,
    MEMORY_SCOPE_DEVICE,
    STORAGE_VERSION,
)

from ._compat import external_tool_call_delta, external_tool_result_delta
from ._sse import BackendStreamError, async_iter_sse
from .base import (
    BackendAdapter,
    BackendAuthError,
    BackendConnectionError,
    BackendStatusError,
    DeltaStream,
    bearer_headers,
    build_client_timeout,
    loads_json_object,
    probe_json,
    trace_token_stats,
)

if TYPE_CHECKING:
    from homeassistant.components import conversation

    from .base import TurnContext

_LOGGER = logging.getLogger(__name__)

# Per-entry discriminator key (rides along on connection_data from __init__.py setup).
_CONF_ENTRY_ID = "entry_id"

# Default Dify end-user: every HA conversation shares it; conversation ids are globally
# unique so scopes cannot collide. CONF_DIFY_USER_FROM_HA swaps in the HA user id.
DEFAULT_USER = "home-assistant"

# App modes the chat API serves (spec AppInfoResponse.mode): ``chat`` Chatbot,
# ``agent-chat`` Legacy Agent, ``advanced-chat`` Chatflow, ``agent`` Agent. ``workflow`` /
# ``completion`` apps need other routes (chat-messages answers them 400 ``not_chat_app``).
_CHAT_APP_MODES = frozenset({"chat", "agent-chat", "advanced-chat", "agent"})

# Wire event names. ``agent_message`` (Agent apps) and ``message`` (Chatbot/Chatflow)
# both carry answer deltas; the rest are terminal/ignored per the module docstring.
_EVENT_AGENT_MESSAGE = "agent_message"
_EVENT_MESSAGE = "message"
_CONTENT_EVENTS = frozenset({_EVENT_AGENT_MESSAGE, _EVENT_MESSAGE})
_EVENT_MESSAGE_END = "message_end"
_EVENT_ERROR = "error"
_EVENT_MESSAGE_REPLACE = "message_replace"
_EVENT_REASONING_CHUNK = "reasoning_chunk"
_EVENT_WORKFLOW_PAUSED = "workflow_paused"
_EVENT_AGENT_THOUGHT = "agent_thought"
_EVENT_NODE_STARTED = "node_started"
_EVENT_NODE_FINISHED = "node_finished"
_NODE_EVENTS = frozenset({_EVENT_NODE_STARTED, _EVENT_NODE_FINISHED})
# Chatflow node statuses that mean the tool failed (StreamEventNodeFinished.status).
_NODE_FAILED = frozenset({"failed", "exception"})

# Spoken when a Chatflow pauses at a Human Input node (see module docstring).
PAUSED_NOTICE = "The assistant paused and is waiting for input in Dify."

# HA turn context offered to the app as ``inputs`` (CONF_DIFY_HA_CONTEXT). Sent only for
# variables the app declares in its user_input_form, so an app that declares none is
# unaffected; ``ha_extra_system_prompt`` carries a satellite start_conversation/ask_question
# prompt (HA keeps it on the chat log across the follow-up turns).
INPUT_LANGUAGE = "ha_language"
INPUT_DEVICE_ID = "ha_device_id"
INPUT_AREA = "ha_area"
INPUT_SATELLITE_ID = "ha_satellite_id"
INPUT_EXTRA_SYSTEM_PROMPT = "ha_extra_system_prompt"
HA_CONTEXT_INPUTS = (INPUT_LANGUAGE, INPUT_DEVICE_ID, INPUT_AREA, INPUT_SATELLITE_ID, INPUT_EXTRA_SYSTEM_PROMPT)

# How long a fetched ``GET /parameters`` user_input_form is reused before re-fetching.
_PARAMETERS_TTL = 300.0

# Operator hints for documented chat-messages error codes (spec POST /chat-messages 400/429);
# the turn still falls back, but the log names the cause instead of a bare status.
_ERROR_HINTS = {
    "too_many_requests": "Dify rate-limited this app (too many concurrent requests)",
    "rate_limit_error": "the Dify Cloud workflow execution quota is exhausted",
    "invalid_param": "Dify rejected the request parameters; map required app inputs in the agent's Dify inputs option",
    "agent_not_published": "the Dify Agent has no published version; publish it in Dify",
    "not_chat_app": "the API key belongs to a Dify workflow/completion app, not a chat app",
}

# Config-flow error keys raised via :class:`DifyAppError`.
ERROR_NOT_CHAT_APP = "not_chat_app"
ERROR_AGENT_NOT_PUBLISHED = "agent_not_published"


class DifyAppError(BackendConnectionError):
    """The key is valid but the app cannot serve chat; ``error_key`` names the form error."""

    def __init__(self, error_key: str, message: str) -> None:
        """Store the config-flow error key alongside the log message."""
        super().__init__(message)
        self.error_key = error_key


class _ConversationGone(Exception):
    """Internal signal: the echoed ``conversation_id`` is deleted/ended — recreate it."""


def _is_stale_conversation(status: int, code: object, message: str) -> bool:
    """True when a pre-stream error means the echoed conversation can't be continued.

    404: Dify has shipped more than one wire shape for a deleted id — the spec's
    ``code: not_found`` + "Conversation Not Exists." and an older ``conversation_not_exists``
    code. 400 ``conversation_completed``: "the conversation has ended; start a new one by
    omitting conversation_id" (spec POST /chat-messages 400).
    """
    if status == 404:
        return code in ("conversation_not_exists", "not_found") or "conversation not exist" in message.lower()
    return status == 400 and code == "conversation_completed"


def _parse_user_input_form(form: object) -> dict[str, dict[str, Any]]:
    """Map ``variable -> control`` from a ``user_input_form`` (``[{"text-input": {...}}, ...]``)."""
    declared: dict[str, dict[str, Any]] = {}
    if not isinstance(form, list):
        return declared
    for item in cast("list[object]", form):
        if not isinstance(item, dict) or len(cast("dict[str, object]", item)) != 1:
            continue
        control = next(iter(cast("dict[str, object]", item).values()))
        if not isinstance(control, dict):
            continue
        control = cast("dict[str, Any]", control)
        variable = control.get("variable")
        if isinstance(variable, str) and variable:
            declared[variable] = control
    return declared


def _fit_input(value: str, control: Mapping[str, Any]) -> str:
    """Trim ``value`` to the control's ``max_length`` (Dify 400s an over-long input)."""
    max_length = control.get("max_length")
    if isinstance(max_length, int) and max_length > 0 and len(value) > max_length:
        _LOGGER.debug("Truncating Dify input %s to its max_length %d", control.get("variable"), max_length)
        return value[:max_length]
    return value


async def _fetch_parameters(session: aiohttp.ClientSession, base_url: str, api_key: str | None) -> dict[str, Any]:
    """``GET {base}/parameters`` (spec "Get App Parameters") and return its JSON body.

    401/403 -> :class:`BackendAuthError`; 400 ``agent_not_published`` -> :class:`DifyAppError`;
    any other error status -> :class:`BackendStatusError`; transport failure or a non-object
    body -> :class:`BackendConnectionError`.
    """
    url = f"{base_url}/parameters"
    try:
        async with session.get(
            url, headers=bearer_headers(api_key), timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
        ) as resp:
            if resp.status in (401, 403):
                raise BackendAuthError(f"Dify rejected the API key (HTTP {resp.status})")
            body = await resp.text()
    except (TimeoutError, aiohttp.ClientError) as err:
        raise BackendConnectionError(f"Cannot reach Dify at {url}: {err}") from err
    payload = loads_json_object(body)
    if resp.status >= 400:
        code = (payload or {}).get("code")
        if code == "agent_not_published":
            raise DifyAppError(ERROR_AGENT_NOT_PUBLISHED, "Dify Agent has no published version")
        raise BackendStatusError(f"Dify parameters probe returned HTTP {resp.status}: {code or 'unknown'}")
    if payload is None:
        raise BackendConnectionError("Dify parameters probe returned a non-object body")
    return payload


@dataclass(frozen=True)
class _ToolActivity:
    """One backend-run tool call as seen so far: args once known, result once finished."""

    call_id: str
    name: str
    args: dict[str, Any] | None
    result: Any = None
    error: bool = False


def _loads_any(raw: object) -> object:
    """Parse a JSON string field, or ``None`` when it is empty/not JSON."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _thought_tools(payload: Mapping[str, Any]) -> list[_ToolActivity]:
    """Tool activity of an ``agent_thought`` (spec StreamEventChatAgentThought).

    ``tool`` lists the tools ``;``-separated; ``tool_input`` is JSON — for Dify agents an
    object keyed by tool name (``{"dalle3": {"prompt": ...}}``); ``observation`` is the
    tools' output, likewise keyed by tool name when several ran. Ids are the thought id +
    tool name, stable across the repeated updates of one thought.
    """
    names = [name.strip() for name in str(payload.get("tool") or "").split(";") if name.strip()]
    if not names:
        return []
    thought_id = str(payload.get("id") or payload.get("position") or "thought")
    raw_input, raw_observation = payload.get("tool_input"), payload.get("observation")
    inputs, observation = _loads_any(raw_input), _loads_any(raw_observation)
    activities: list[_ToolActivity] = []
    for name in names:
        args: dict[str, Any] | None = None
        if isinstance(raw_input, str) and raw_input.strip():
            keyed = cast("dict[str, Any]", inputs).get(name) if isinstance(inputs, dict) else None
            if isinstance(keyed, dict):
                args = cast("dict[str, Any]", keyed)
            elif keyed is not None:
                args = {"input": keyed}
            elif isinstance(inputs, dict) and len(names) == 1:
                args = cast("dict[str, Any]", inputs)
            else:
                args = {"input": raw_input}
        result: Any = None
        if isinstance(raw_observation, str) and raw_observation.strip():
            by_tool = cast("dict[str, Any]", observation) if isinstance(observation, dict) else {}
            result = by_tool.get(name, raw_observation)
        activities.append(_ToolActivity(f"{thought_id}:{name}", name, args, result))
    return activities


def _node_tool(event: str, payload: Mapping[str, Any]) -> _ToolActivity | None:
    """Tool activity of a Chatflow ``node_started``/``node_finished`` for a ``tool`` node."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    data = cast("dict[str, Any]", data)
    call_id = data.get("id")
    if data.get("node_type") != "tool" or not isinstance(call_id, str) or not call_id:
        return None
    name = str(data.get("title") or data.get("node_id") or "tool")
    inputs = data.get("inputs")
    args = cast("dict[str, Any]", inputs) if isinstance(inputs, dict) else {}
    if event != _EVENT_NODE_FINISHED:
        return _ToolActivity(call_id, name, args)
    if data.get("status") in _NODE_FAILED:
        return _ToolActivity(call_id, name, args, {"error": data.get("error") or "tool failed"}, error=True)
    outputs = data.get("outputs")
    return _ToolActivity(call_id, name, args, outputs if isinstance(outputs, dict) else {})


def _end_user(options: Mapping[str, Any], user_input: conversation.ConversationInput) -> str:
    """Return the Dify ``user``: the HA user id when opted in and known, else the default.

    Voice-satellite turns carry no HA user, so they keep the shared default identity.
    """
    if options.get(CONF_DIFY_USER_FROM_HA) and user_input.context.user_id:
        return user_input.context.user_id
    return DEFAULT_USER


def _trace_usage(chat_log: conversation.ChatLog, message_end: Mapping[str, Any]) -> None:
    """Record ``message_end.metadata.usage`` in the conversation trace.

    Token counts go through the shared :func:`trace_token_stats` (core's ``stats`` shape)
    when Dify reports both; the full usage (price, currency, latency) is traced as
    ``dify_usage``. The spec's Agent example carries only ``total_tokens``/``latency``.
    """
    metadata = message_end.get("metadata")
    usage = cast("dict[str, Any]", metadata).get("usage") if isinstance(metadata, dict) else None
    if not isinstance(usage, dict):
        return
    usage = cast("dict[str, Any]", usage)
    prompt_tokens, completion_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
        trace_token_stats(chat_log, input_tokens=prompt_tokens, output_tokens=completion_tokens)
    chat_log.async_trace({"dify_usage": usage})


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _tag_tail(text: str, tag: str) -> int:
    """Length of the longest suffix of ``text`` that is a proper prefix of ``tag``.

    Lets the streaming stripper hold back a trailing fragment that might be the
    start of a tag split across chunk boundaries (e.g. ``"…<th"``) without
    emitting it prematurely, while a fragment that turns out to be literal text
    (``"< 5"``) matches nothing and flushes normally.
    """
    for k in range(min(len(text), len(tag) - 1), 0, -1):
        if text[-k:] == tag[:k]:
            return k
    return 0


class _ThinkStripper:
    """Stateful streaming filter that removes ``<think>…</think>`` reasoning.

    Reasoning models inline chain-of-thought as ``<think>…</think>`` in the
    ``answer`` content; an agent-chat app has no node to strip it, so it would be
    spoken aloud by the voice pipeline. ``feed`` returns only the visible text
    outside think blocks, correctly handling multiple blocks and tags split
    across deltas/chunks. An unclosed block at end-of-stream is dropped (its
    partial reasoning is never emitted).
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, chunk: str) -> str:
        """Consume a content delta; return the emittable (non-reasoning) text."""
        self._buf += chunk
        out: list[str] = []
        while self._buf:
            if not self._in_think:
                idx = self._buf.find(_THINK_OPEN)
                if idx == -1:
                    # Emit all but a possible split-open fragment kept for next feed.
                    hold = _tag_tail(self._buf, _THINK_OPEN)
                    out.append(self._buf[: len(self._buf) - hold])
                    self._buf = self._buf[len(self._buf) - hold :]
                    break
                out.append(self._buf[:idx])
                self._buf = self._buf[idx + len(_THINK_OPEN) :]
                self._in_think = True
            else:
                idx = self._buf.find(_THINK_CLOSE)
                if idx == -1:
                    # Inside a block: drop everything but a possible split-close fragment.
                    hold = _tag_tail(self._buf, _THINK_CLOSE)
                    self._buf = self._buf[len(self._buf) - hold :]
                    break
                self._buf = self._buf[idx + len(_THINK_CLOSE) :]
                self._in_think = False
        return "".join(out)

    def flush(self) -> str:
        """End of stream: emit a held non-tag fragment; drop any unclosed block."""
        tail = "" if self._in_think else self._buf
        self._buf = ""
        return tail


class DifyAdapter(BackendAdapter):
    """Dify preset: streaming-only chat API, server-side memory, no HA tools."""

    backend_type: ClassVar[str] = BACKEND_DIFY
    supports_ha_tools: ClassVar[bool] = False  # tools live in the Dify app
    supports_memory_scope: ClassVar[bool] = True  # stateful -> key maps to a conversation_id

    @classmethod
    def store_keys(cls, entry_key: str) -> tuple[str, ...]:
        """The device/agent-scope conversation map."""
        return (f"{DOMAIN}.dify.{slugify(entry_key)}",)

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        connection_data: Mapping[str, Any],
    ) -> None:
        """Store connection state and build the per-entry conversation-map store."""
        super().__init__(hass, session, connection_data)
        self._base_url = str(connection_data.get(CONF_BASE_URL, "")).rstrip("/")
        self._api_key: str | None = connection_data.get(CONF_API_KEY) or None
        # Per-entry discriminator for the Store filename (entry_id preferred; base_url
        # slug is a collision-safe fallback for a construction path without an entry_id).
        discriminator = connection_data.get(_CONF_ENTRY_ID) or self._base_url or "default"
        (store_key,) = self.store_keys(str(discriminator))
        self._store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, store_key)
        # conversation-scope map (in-memory) and lazily loaded device/agent-scope map.
        self._mem: dict[str, str] = {}
        self._persisted: dict[str, str] | None = None
        self._lock = asyncio.Lock()
        # (fetched_at monotonic, variable -> control) from GET /parameters; per entry = per app.
        self._form_cache: tuple[float, dict[str, dict[str, Any]]] | None = None

    def _headers(self) -> dict[str, str]:
        """Build request headers; the Dify app key is a required bearer token."""
        return {"Content-Type": "application/json", **bearer_headers(self._api_key)}

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        """Probe ``GET {base}/info`` then ``GET {base}/parameters``.

        401/403 -> :class:`BackendAuthError`; transport failure, any other error status, or
        a non-object body -> :class:`BackendConnectionError`. An ``/info`` ``mode`` that is
        not a chat-type app (workflow/completion) -> :class:`DifyAppError` ``not_chat_app``;
        ``/parameters`` 400 ``agent_not_published`` (an Agent app whose Agent was never
        published, which ``/info`` does not reveal) -> :class:`DifyAppError`
        ``agent_not_published``.
        """
        session = async_get_clientsession(hass)
        base_url = str(data.get(CONF_BASE_URL, "")).rstrip("/")
        api_key = data.get(CONF_API_KEY) or None
        headers = bearer_headers(api_key)
        payload = await probe_json(session, f"{base_url}/info", label="Dify", headers=headers)
        mode = payload.get("mode")
        if isinstance(mode, str) and mode not in _CHAT_APP_MODES:
            raise DifyAppError(ERROR_NOT_CHAT_APP, f"Dify app mode {mode!r} is not a chat app")
        await _fetch_parameters(session, base_url, api_key)

    @classmethod
    async def async_fetch_input_form(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Return the app's declared ``inputs`` (``variable -> control``) for the agent form."""
        base_url = str(data.get(CONF_BASE_URL, "")).rstrip("/")
        params = await _fetch_parameters(async_get_clientsession(hass), base_url, data.get(CONF_API_KEY) or None)
        return _parse_user_input_form(params.get("user_input_form"))

    async def stream_turn(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        ctx: TurnContext,
    ) -> DeltaStream:
        """Send the new turn to ``{base}/chat-messages`` and stream its SSE reply.

        Dify owns history server-side, so only the new turn is sent, keyed on
        ``ctx.memory_key`` -> the mapped ``conversation_id``; ``chat_log`` is used for the
        satellite extra prompt (HA context inputs) and trace records only. A stale or ended
        id rejected pre-stream is dropped and the turn retried once without one.
        """
        options = ctx.options
        timeout = build_client_timeout(options)
        scope = str(options.get(CONF_MEMORY_SCOPE, MEMORY_SCOPE_CONVERSATION))
        persist = scope in (MEMORY_SCOPE_DEVICE, MEMORY_SCOPE_AGENT)
        session_key = ctx.memory_key
        user = _end_user(options, user_input)
        body: dict[str, Any] = {
            "query": user_input.text,
            "inputs": await self._build_inputs(chat_log, user_input, options),
            "response_mode": "streaming",
            "user": user,
            # Title generation is an extra LLM call per new conversation (one per voice
            # command under conversation scope) competing with the next turn on local models.
            "auto_generate_name": False,
        }

        conversation_id = await self._lookup(session_key, persist, ctx.legacy_memory_key)
        # ``task_id`` is captured inside ``_run_stream`` but read here: GeneratorExit is
        # thrown into *this* generator's yield on cancellation, so the best-effort stop
        # must fire from this frame (deterministic, not via the inner generator's GC).
        state: dict[str, str | None] = {"task_id": None}
        try:
            try:
                async for delta in self._run_stream(
                    chat_log, body, conversation_id, session_key, persist, timeout, state
                ):
                    yield delta
            except _ConversationGone:
                # Echoed id was deleted/ended server-side: drop the mapping and retry once.
                _LOGGER.debug("Dify conversation %s gone; recreating", conversation_id)
                await self._drop(session_key, persist)
                async for delta in self._run_stream(chat_log, body, None, session_key, persist, timeout, state):
                    yield delta
        except GeneratorExit, asyncio.CancelledError:
            self._stop_started_task(state, user)
            raise
        except Exception:
            # Idle timeout, dropped stream, in-stream error, ...: free the Dify task too.
            self._stop_started_task(state, user)
            raise

    async def _run_stream(
        self,
        chat_log: conversation.ChatLog,
        body: Mapping[str, Any],
        conversation_id: str | None,
        session_key: str,
        persist: bool,
        timeout: aiohttp.ClientTimeout,
        state: dict[str, str | None],
    ) -> DeltaStream:
        """POST one turn and yield role-first deltas from the SSE reply.

        Captures ``conversation_id``/``task_id`` from the first event carrying them (any
        event type — a Chatflow's ``workflow_started``/``node_*`` precede the answer) and
        persists the id mid-stream; ``task_id`` goes into ``state`` for the caller's
        best-effort stop. Raises :class:`_ConversationGone` on a pre-stream stale/ended
        conversation (so the caller recreates); :class:`BackendAuthError` /
        :class:`BackendConnectionError` on other pre-stream failures;
        :class:`BackendStreamError` on an in-stream ``error`` event or an EOF without a
        terminal event.
        """
        request = dict(body)
        if conversation_id is not None:
            request["conversation_id"] = conversation_id

        block_open = False  # an assistant block that may take more content is open
        content_sent = False
        needs_reply = False  # tool activity happened since the last spoken text
        replaced = False
        captured = False
        agent_streamed = False
        stripper = _ThinkStripper()
        calls_sent: set[str] = set()
        results_sent: set[str] = set()

        def opened(delta: conversation.AssistantContentDeltaDict) -> list[conversation.AssistantContentDeltaDict]:
            # Role-first invariant: the first delta of each block carries the role.
            nonlocal block_open, needs_reply
            if "content" in delta:
                needs_reply = False
            if block_open:
                return [delta]
            block_open = True
            return [{"role": "assistant"}, delta]

        def tool_deltas(activity: _ToolActivity) -> list[conversation.AssistantContentDeltaDict]:
            # Agent thoughts repeat as they fill in, so each call/result is emitted once; a
            # result never precedes its call.
            nonlocal block_open, needs_reply
            deltas: list[conversation.AssistantContentDeltaDict] = []
            if activity.call_id not in calls_sent and (activity.args is not None or activity.result is not None):
                calls_sent.add(activity.call_id)
                deltas.append(external_tool_call_delta(activity.call_id, activity.name, activity.args or {}))
            if activity.result is not None and activity.call_id not in results_sent:
                results_sent.add(activity.call_id)
                deltas.append(
                    external_tool_result_delta(activity.call_id, activity.name, activity.result, error=activity.error)
                )
            if deltas:
                block_open, needs_reply = False, True
            return deltas

        async def _on_error_status(resp: aiohttp.ClientResponse) -> None:
            if resp.status in (401, 403):
                raise BackendAuthError(f"Dify rejected the API key (HTTP {resp.status})")
            detail = loads_json_object(await resp.text()) or {}
            code = detail.get("code")
            if conversation_id is not None and _is_stale_conversation(
                resp.status, code, str(detail.get("message") or "")
            ):
                raise _ConversationGone
            if resp.status == 404:
                raise BackendConnectionError(f"Dify chat-messages 404: {code or 'unknown'}")
            hint = _ERROR_HINTS.get(str(code))
            if hint is not None:
                _LOGGER.warning("Dify chat-messages HTTP %s (%s): %s", resp.status, code, hint)

        async with self._post_checked(
            f"{self._base_url}/chat-messages",
            label="Dify chat-messages",
            data=json.dumps(request),
            headers=self._headers(),
            timeout=timeout,
            error_cls=BackendConnectionError,
            on_error_status=_on_error_status,
        ) as resp:
            async for sse in async_iter_sse(resp.content.iter_any()):
                payload = loads_json_object(sse.data, warn_label="Dify") or {}
                # Dify puts the event name in the JSON (no ``event:`` line, so the framer's
                # default "message" is meaningless); the SSE field is only a proxy fallback.
                json_event = payload.get("event")
                event = json_event if isinstance(json_event, str) else sse.event
                if state["task_id"] is None:
                    tid = payload.get("task_id")
                    if isinstance(tid, str) and tid:
                        state["task_id"] = tid
                if not captured:
                    cid = payload.get("conversation_id")
                    if isinstance(cid, str) and cid:
                        captured = True
                        if cid != conversation_id:
                            await self._remember(session_key, cid, persist)
                if event == _EVENT_ERROR:
                    _LOGGER.error("Dify error event: code=%s message=%s", payload.get("code"), payload.get("message"))
                    raise BackendStreamError(f"Dify error event: {payload.get('code')}")
                if event == _EVENT_MESSAGE_END:
                    tail = stripper.flush()
                    if tail and not replaced:
                        for delta in opened({"content": tail}):
                            yield delta
                    _trace_usage(chat_log, payload)
                    if needs_reply:
                        # Ending on a tool result would re-enter the entity's tool loop and
                        # re-send the turn; raising lets the guard append the fallback reply.
                        state["task_id"] = None  # the task finished; nothing to stop
                        raise BackendStreamError("Dify run used tools but produced no reply text")
                    return
                if event == _EVENT_WORKFLOW_PAUSED:
                    _LOGGER.warning("Dify chatflow paused waiting for human input; finish it in Dify")
                    chat_log.async_trace({"dify_workflow_paused": payload.get("data")})
                    notice = f" {PAUSED_NOTICE}" if content_sent else PAUSED_NOTICE
                    for delta in opened({"content": notice}):
                        yield delta
                    return
                if event == _EVENT_MESSAGE_REPLACE:
                    replaced = True
                    replacement = payload.get("answer")
                    chat_log.async_trace(
                        {"dify_message_replace": {"answer": replacement, "reason": payload.get("reason")}}
                    )
                    if not content_sent and isinstance(replacement, str) and replacement:
                        content_sent = True
                        for delta in opened({"content": replacement}):
                            yield delta
                    else:
                        _LOGGER.warning(
                            "Dify moderation replaced an answer that was already spoken; "
                            "the replacement is recorded in the conversation trace only"
                        )
                    continue
                if event == _EVENT_REASONING_CHUNK:
                    data = payload.get("data")
                    reasoning = cast("dict[str, Any]", data).get("reasoning") if isinstance(data, dict) else None
                    if isinstance(reasoning, str) and reasoning:
                        for delta in opened({"thinking_content": reasoning}):
                            yield delta
                    continue
                if event == _EVENT_AGENT_THOUGHT:
                    for activity in _thought_tools(payload):
                        for delta in tool_deltas(activity):
                            yield delta
                    continue
                if event in _NODE_EVENTS:
                    node_activity = _node_tool(event, payload)
                    if node_activity is not None:
                        for delta in tool_deltas(node_activity):
                            yield delta
                    continue
                if event not in _CONTENT_EVENTS:
                    continue  # message_file / tts_* / workflow_* / node_* / unknown
                if event == _EVENT_AGENT_MESSAGE:
                    agent_streamed = True
                elif agent_streamed:
                    # Agent apps close with one ``message`` holding the complete answer.
                    continue
                answer = payload.get("answer")
                if replaced or not isinstance(answer, str) or not answer:
                    continue  # after a moderation replace, the original answer is dropped
                visible = stripper.feed(answer)
                if visible:
                    content_sent = True
                    for delta in opened({"content": visible}):
                        yield delta
        # Every documented outcome ends in message_end / error / workflow_paused; plain EOF
        # is a dropped connection, so a partial reply must not pass as a complete one.
        raise BackendStreamError("Dify stream ended without message_end")

    # --- inputs -----------------------------------------------------------------

    async def _build_inputs(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        options: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Assemble ``inputs``: declared HA-context variables, then the static mapping."""
        inputs: dict[str, Any] = {}
        if options.get(CONF_DIFY_HA_CONTEXT):
            declared = await self._declared_inputs()
            for variable, value in self._ha_context(chat_log, user_input, declared).items():
                inputs[variable] = _fit_input(value, declared[variable])
        static = options.get(CONF_DIFY_INPUTS)
        if isinstance(static, Mapping):
            inputs.update(cast("Mapping[str, Any]", static))
        return inputs

    async def _declared_inputs(self) -> dict[str, dict[str, Any]]:
        """Return the app's user_input_form variables, re-fetched after :data:`_PARAMETERS_TTL`."""
        now = time.monotonic()
        if self._form_cache is not None and now - self._form_cache[0] < _PARAMETERS_TTL:
            return self._form_cache[1]
        try:
            params = await _fetch_parameters(self.session, self._base_url, self._api_key)
        except BackendConnectionError as err:
            # Context inputs are optional enrichment: never fail the turn over them.
            _LOGGER.warning("Dify parameters fetch failed; sending no HA context inputs: %s", err)
            return {}
        declared = _parse_user_input_form(params.get("user_input_form"))
        self._form_cache = (now, declared)
        return declared

    def _ha_context(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        declared: Mapping[str, Any],
    ) -> dict[str, str]:
        """Return the non-empty HA context values for the ``ha_*`` variables the app declares."""
        sources: dict[str, Callable[[], str | None]] = {
            INPUT_LANGUAGE: lambda: user_input.language,
            INPUT_DEVICE_ID: lambda: user_input.device_id,
            INPUT_AREA: lambda: self._area_name(user_input.device_id),
            INPUT_SATELLITE_ID: lambda: user_input.satellite_id,
            # The chat log keeps a start_conversation/ask_question prompt for follow-up turns.
            INPUT_EXTRA_SYSTEM_PROMPT: lambda: chat_log.extra_system_prompt or user_input.extra_system_prompt,
        }
        values: dict[str, str] = {}
        for variable, source in sources.items():
            if variable in declared and (value := source()):
                values[variable] = value
        return values

    def _area_name(self, device_id: str | None) -> str | None:
        """Return the area name of ``device_id`` (the satellite), or ``None``."""
        if device_id is None:
            return None
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None or device.area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(device.area_id)
        return area.name if area is not None else None

    # --- best-effort stop -------------------------------------------------------

    def _stop_started_task(self, state: Mapping[str, str | None], user: str) -> None:
        """Fire a stop for the turn's Dify task if one was seen (never raises)."""
        task_id = state["task_id"]
        if task_id is not None:
            self._fire_stop(task_id, user)

    def _fire_stop(self, task_id: str, user: str) -> None:
        """Schedule a fire-and-forget ``stop`` for ``task_id`` as the turn's end-user."""
        self.hass.async_create_background_task(self._stop(task_id, user), name=f"{DOMAIN}.dify.stop.{task_id}")

    async def _stop(self, task_id: str, user: str) -> None:
        """POST ``/chat-messages/{task_id}/stop``; swallow errors (best-effort abort).

        ``user`` must match the turn's ``user`` — otherwise the spec says the stop
        "silently does nothing and still returns success".
        """
        url = f"{self._base_url}/chat-messages/{task_id}/stop"
        try:
            async with self.session.post(
                url,
                data=json.dumps({"user": user}),
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ):
                return
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("Dify stop request failed for task %s: %s", task_id, err)

    # --- conversation mapping ---------------------------------------------------

    async def _lookup(self, session_key: str, persist: bool, legacy_key: str | None = None) -> str | None:
        """Return the mapped ``conversation_id`` for ``session_key``, or ``None``.

        A persisted entry under ``legacy_key`` (the pre-per-agent device key) moves to
        ``session_key`` on first use, so one agent keeps it and siblings start fresh.
        """
        if not persist:
            return self._mem.get(session_key)
        async with self._lock:
            mapping = await self._persisted_map()
            if session_key not in mapping and legacy_key is not None and legacy_key in mapping:
                mapping[session_key] = mapping.pop(legacy_key)
                await self._store.async_save(mapping)
            return mapping.get(session_key)

    async def _remember(self, session_key: str, conversation_id: str, persist: bool) -> None:
        """Record ``session_key -> conversation_id`` in the in-memory or persisted map."""
        if not persist:
            self._mem[session_key] = conversation_id
            return
        async with self._lock:
            mapping = await self._persisted_map()
            mapping[session_key] = conversation_id
            await self._store.async_save(mapping)

    async def _drop(self, session_key: str, persist: bool) -> None:
        """Forget ``session_key``'s mapping after the server rejected the id (404)."""
        if not persist:
            self._mem.pop(session_key, None)
            return
        async with self._lock:
            mapping = await self._persisted_map()
            if mapping.pop(session_key, None) is not None:
                await self._store.async_save(mapping)

    async def _persisted_map(self) -> dict[str, str]:
        """Lazily load the persisted device/agent-scope map (call under ``self._lock``)."""
        if self._persisted is None:
            loaded = await self._store.async_load()
            self._persisted = loaded if loaded is not None else {}
        return self._persisted
