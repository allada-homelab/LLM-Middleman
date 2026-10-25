"""LangGraph Agent Server backend adapter (LLMM-011, tier-1 rework LLMM-022).

Stateful-thread preset: the graph owns its history and tools server-side, so this
adapter sends only the **new** turn on a thread derived from ``ctx.memory_key`` and
streams the reply back. Text-only passthrough (``supports_ha_tools = False``); the same
wire API serves ``langgraph dev``, self-hosted, and LangSmith cloud deployments.

Wire behaviour (confirmed against a live ``langgraph-api`` 0.16.6 server, see
``docs/implementation/e2e-results/langgraph-tier1.md``):

* Runs stream with ``stream_mode=["messages-tuple", "updates"]``. ``messages`` frames are
  ``[message, metadata]`` pairs; only AI messages (``AIMessageChunk`` tokens, or a whole
  ``ai`` message from a node that did not stream) are spoken — ``tool``/``human`` messages
  and tool-call-only chunks never reach TTS. ``updates`` frames carry each node's
  complete output: finished tool calls and tool results (surfaced in the chat log as
  external tool activity), ``usage_metadata`` (traced as token stats), and
  ``__interrupt__`` (a graph waiting on ``interrupt()``).
* A successful run ends at SSE EOF (no terminal event); ``event: error`` raises. The
  server sends a ``: heartbeat`` comment every 5 s, so a long silent tool step never
  trips the agent timeout (``sock_read``).
* Threads are deterministic: ``thread_id = uuid5(namespace, "<entry_id>:<memory_key>")``
  and every run sends ``if_not_exists: "create"``, so the server creates the thread on
  first use and re-creates one it has deleted — no client-side map, lock, or 404 retry.
  A ``session_key -> thread_id`` map persisted by the LLMM-011 adapter is still honoured
  so existing device/agent-scope threads keep their history (a device entry is adopted by
  the first agent that uses it, since device keys are now per agent).
* ``on_disconnect: "cancel"`` stops a run HA abandoned (cancelled turn, timeout);
  a best-effort ``POST /threads/{id}/runs/{run_id}/cancel`` backs it up for proxies
  that keep the upstream connection open. ``multitask_strategy`` (default ``rollback``)
  decides what a new turn does to a run still in flight on the same thread.
* An interrupt is spoken (its string value, or ``question``/``message``), follow-up
  listening is requested, and the next turn on that thread is sent as
  ``{"command": {"resume": <reply>}}`` after confirming the thread still has a pending
  interrupt.

Graph-side contract: each run's ``context`` carries ``system_prompt`` (threaded runs —
a system message in ``input`` would accumulate in thread state every turn), ``language``,
``device_id``, ``satellite_id``, ``conversation_id`` and ``user_id`` (keys without a value
are omitted). Graphs read them from ``runtime.context``; unknown keys are dropped by a
dataclass ``context_schema``, so declaring only the ones a graph needs is fine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from custom_components.llm_middleman.const import (
    BACKEND_LANGGRAPH,
    CONF_API_KEY,
    CONF_ASSISTANT_ID,
    CONF_AUTH_SCHEME,
    CONF_BASE_URL,
    CONF_INPUT_MESSAGES_KEY,
    CONF_MEMORY_SCOPE,
    CONF_MULTITASK_STRATEGY,
    CONF_RESPONSE_NODE_FILTER,
    CONF_STATELESS_RUNS,
    DEFAULT_ASSISTANT_ID,
    DEFAULT_INPUT_MESSAGES_KEY,
    DEFAULT_MULTITASK_STRATEGY,
    DEFAULT_TIMEOUT,
    DOMAIN,
    LANGGRAPH_AUTH_BEARER,
    MEMORY_SCOPE_AGENT,
    MEMORY_SCOPE_CONVERSATION,
    MEMORY_SCOPE_DEVICE,
    MULTITASK_STRATEGIES,
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
    json_list,
    json_object,
    rendered_system_prompt,
    trace_token_stats,
)

if TYPE_CHECKING:
    from homeassistant.components import conversation

    from .base import TurnContext

_LOGGER = logging.getLogger(__name__)

_CONF_ENTRY_ID = "entry_id"

# Wire event names. Success terminates by SSE EOF, so there is no terminal event.
_EVENT_ERROR = "error"
_EVENT_MESSAGES = "messages"
_EVENT_METADATA = "metadata"
_EVENT_UPDATES = "updates"
_INTERRUPT_KEY = "__interrupt__"

# LangChain message ``type`` values as serialized on the wire: streamed tokens are
# ``AIMessageChunk``; a node that returns a message without streaming emits ``ai``.
_AI_TYPES = frozenset({"ai", "AIMessage", "AIMessageChunk"})
_TOOL_TYPES = frozenset({"tool", "ToolMessage"})

# Thread ids are uuid5 under this namespace. It must never change: existing users'
# threads are addressed by the ids it produces.
# (= uuid5(NAMESPACE_URL, "https://github.com/allada-homelab/LLM-Middleman#langgraph-thread")).
_THREAD_NAMESPACE = uuid.UUID("75e17f5a-ab70-5736-83d6-5c5f8ce93dca")

# The best-effort cancel must not outlive the turn by much; it never blocks the turn.
_CANCEL_TIMEOUT = 5

# Spoken when a graph interrupts with a value that has no readable question.
_INTERRUPT_FALLBACK = "The assistant needs more information to continue."


class AssistantNotFoundError(BackendConnectionError):
    """The configured assistant/graph id does not exist on the deployment."""


def _headers(api_key: str | None, auth_scheme: str | None) -> dict[str, str]:
    """Build request headers; attach the key as ``x-api-key`` or a bearer token."""
    headers = {"Content-Type": "application/json"}
    if not api_key:
        return headers
    if auth_scheme == LANGGRAPH_AUTH_BEARER:
        return {**headers, **bearer_headers(api_key)}
    headers["x-api-key"] = api_key
    return headers


def _extract_text(content: object) -> str:
    """Pull plain text from a LangChain message's ``content``.

    ``content`` is a string for text models, or a list of content blocks (str or
    ``{"type": "text", "text": ...}``) for block-based models. Non-text blocks (tool use,
    images) are skipped so only speakable text reaches TTS.
    """
    if isinstance(content, str):
        return content
    if (blocks := json_list(content)) is not None:
        parts: list[str] = []
        for block in blocks:
            if isinstance(block, str):
                parts.append(block)
            elif (mapping := json_object(block)) is not None:
                text = mapping.get("text")
                if mapping.get("type") == "text" and isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _parse_messages_frame(data: str) -> tuple[str | None, str | None]:
    """Parse a ``messages-tuple`` SSE ``data`` payload into ``(text, node)``.

    Only AI messages yield text: tool results, echoed human input, and tool-call chunks
    (empty ``content``) return ``None`` text so they are never spoken. Malformed JSON or
    an unexpected shape returns ``(None, None)`` so one bad frame is skipped rather than
    aborting the stream.
    """
    try:
        parsed: object = json.loads(data)
    except json.JSONDecodeError, ValueError:
        _LOGGER.debug("Skipping malformed LangGraph messages frame")
        return None, None
    frame = json_list(parsed)
    if frame is None or len(frame) != 2:
        return None, None
    node = (json_object(frame[1]) or {}).get("langgraph_node")
    node = node if isinstance(node, str) else None
    message = json_object(frame[0])
    if message is None:
        return None, node
    if message.get("type") not in _AI_TYPES:
        return None, node
    return _extract_text(message.get("content")) or None, node


@dataclass
class _Updates:
    """What one ``updates`` frame contributes to the turn."""

    tool_calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list[tuple[str, str, dict[str, Any]]])
    tool_results: list[tuple[str, str, Any, bool]] = field(default_factory=list[tuple[str, str, Any, bool]])
    input_tokens: int = 0
    output_tokens: int = 0
    has_usage: bool = False
    interrupt: object = None
    interrupted: bool = False


def _node_messages(update: object, messages_key: str) -> list[dict[str, Any]]:
    """Return the message dicts a node's update wrote to ``messages_key``."""
    if (mapping := json_object(update)) is None:
        return []
    value: object = mapping.get(messages_key)
    messages = json_list(value)
    return [m for item in (messages if messages is not None else [value]) if (m := json_object(item)) is not None]


def _parse_updates_frame(data: str, messages_key: str) -> _Updates:
    """Parse an ``updates`` SSE payload (``{node: update}``) into tool activity/usage/interrupt."""
    out = _Updates()
    try:
        parsed: object = json.loads(data)
    except json.JSONDecodeError, ValueError:
        _LOGGER.debug("Skipping malformed LangGraph updates frame")
        return out
    nodes = json_object(parsed)
    if nodes is None:
        return out
    for node, update in nodes.items():
        if node == _INTERRUPT_KEY:
            interrupts = json_list(update) or []
            first = interrupts[0] if interrupts else None
            out.interrupted = True
            out.interrupt = (json_object(first) or {}).get("value")
            continue
        for message in _node_messages(update, messages_key):
            msg_type = message.get("type")
            if msg_type in _AI_TYPES:
                _collect_ai_message(message, out)
            elif msg_type in _TOOL_TYPES:
                call_id, name = message.get("tool_call_id"), message.get("name")
                if isinstance(call_id, str) and call_id:
                    error = message.get("status") == "error"
                    out.tool_results.append(
                        (call_id, name if isinstance(name, str) else "", message.get("content"), error)
                    )
    return out


def _collect_ai_message(message: dict[str, Any], out: _Updates) -> None:
    """Fold a whole AI message's finished tool calls and token usage into ``out``."""
    for raw in json_list(message.get("tool_calls")) or []:
        if (call := json_object(raw)) is None:
            continue
        call_id, name, args = call.get("id"), call.get("name"), call.get("args")
        if isinstance(call_id, str) and call_id and isinstance(name, str):
            out.tool_calls.append((call_id, name, json_object(args) or {}))
    if (tokens := json_object(message.get("usage_metadata"))) is not None:
        input_tokens, output_tokens = tokens.get("input_tokens"), tokens.get("output_tokens")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            out.input_tokens += input_tokens
            out.output_tokens += output_tokens
            out.has_usage = True


def _interrupt_text(value: object) -> str:
    """Return the speakable question of an interrupt value."""
    if isinstance(value, str) and value:
        return value
    if (mapping := json_object(value)) is not None:
        for key in ("question", "message"):
            text = mapping.get(key)
            if isinstance(text, str) and text:
                return text
    return _INTERRUPT_FALLBACK


@dataclass
class _RunState:
    """Per-run bookkeeping shared between ``stream_turn`` and the stream it drives."""

    run_id: str | None = None
    finished: bool = False
    interrupted: bool = False


async def _probe_assistant(
    session: aiohttp.ClientSession, url: str, headers: Mapping[str, str], body: str | None
) -> object:
    """GET (``body is None``) or POST ``url`` for the assistant check; return the JSON body."""
    timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
    try:
        request = (
            session.get(url, headers=headers, timeout=timeout)
            if body is None
            else session.post(url, data=body, headers=headers, timeout=timeout)
        )
        async with request as response:
            if response.status in (401, 403):
                raise BackendAuthError(f"LangGraph rejected credentials (HTTP {response.status})")
            if response.status == 404:
                raise AssistantNotFoundError(f"LangGraph assistant not found at {url}")
            if response.status >= 400:
                raise BackendStatusError(f"LangGraph assistant probe returned HTTP {response.status}")
            return await response.json()
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        raise BackendConnectionError(f"Cannot reach LangGraph at {url}: {err}") from err


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class LangGraphAdapter(BackendAdapter):
    """LangGraph preset: deterministic threads, ``messages-tuple`` + ``updates`` streaming."""

    backend_type: ClassVar[str] = BACKEND_LANGGRAPH
    supports_ha_tools: ClassVar[bool] = False  # tools live server-side in the graph
    supports_memory_scope: ClassVar[bool] = True  # stateful -> key maps to a thread_id

    @classmethod
    def store_keys(cls, entry_key: str) -> tuple[str, ...]:
        """The legacy thread map and the interrupt->resume pending set."""
        store_key = f"{DOMAIN}.langgraph.{slugify(entry_key)}"
        return (store_key, f"{store_key}.pending_resume")

    # The rendered prompt (with HA's date/time) rides in the run context, and a resume
    # value must be the user's literal answer to the graph's question.
    wants_time_prefix: ClassVar[bool] = False

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        connection_data: Mapping[str, Any],
    ) -> None:
        """Store connection state and the legacy thread-map / pending-resume stores."""
        super().__init__(hass, session, connection_data)
        self._base_url = str(connection_data.get(CONF_BASE_URL, "")).rstrip("/")
        self._request_headers = _headers(
            connection_data.get(CONF_API_KEY) or None, connection_data.get(CONF_AUTH_SCHEME)
        )
        self._assistant_id = str(connection_data.get(CONF_ASSISTANT_ID, DEFAULT_ASSISTANT_ID))
        # Per-entry discriminator for thread ids and Store filenames (entry_id is always
        # passed by async_setup_entry; the base_url slug keeps bare test construction working).
        self._entry_key = str(connection_data.get(_CONF_ENTRY_ID) or self._base_url or "default")
        store_key, pending_key = self.store_keys(self._entry_key)
        # The LLMM-011 session_key -> thread_id map for device/agent scopes; never grows,
        # only re-keys a device entry when an agent adopts it (see _thread_id).
        self._store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, store_key)
        self._legacy: dict[str, str] | None = None
        self._legacy_lock = asyncio.Lock()
        # thread_id -> whether it belongs to a persisted (device/agent) scope.
        self._pending_store: Store[list[str]] = Store(hass, STORAGE_VERSION, pending_key)
        self._pending: dict[str, bool] | None = None
        # Diagnostics only (never read by turn logic, so concurrent turns can't race on it).
        self.last_run_id: str | None = None

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        """Check the deployment answers authenticated requests AND the assistant exists.

        A UUID assistant id is looked up with ``GET /assistants/{id}``; a graph id (which
        that endpoint rejects with 422) with ``POST /assistants/search {"graph_id"}``.
        401/403 -> :class:`BackendAuthError`; missing assistant (404 or no search hit) ->
        :class:`AssistantNotFoundError`; transport failure / other status ->
        :class:`BackendConnectionError`.
        """
        session = async_get_clientsession(hass)
        base_url = str(data.get(CONF_BASE_URL, "")).rstrip("/")
        headers = _headers(data.get(CONF_API_KEY) or None, data.get(CONF_AUTH_SCHEME))
        assistant_id = str(data.get(CONF_ASSISTANT_ID) or DEFAULT_ASSISTANT_ID)
        if _is_uuid(assistant_id):
            await _probe_assistant(session, f"{base_url}/assistants/{assistant_id}", headers, None)
            return
        found = await _probe_assistant(
            session, f"{base_url}/assistants/search", headers, json.dumps({"graph_id": assistant_id, "limit": 1})
        )
        if not (isinstance(found, list) and found):
            raise AssistantNotFoundError(f"LangGraph has no assistant for graph {assistant_id!r}")

    def diagnostics(self) -> dict[str, Any]:
        """Non-secret runtime state for the config-entry diagnostics dump."""
        return {
            "last_run_id": self.last_run_id,
            "threads_awaiting_resume": len(self._pending) if self._pending is not None else None,
            "legacy_thread_map_entries": len(self._legacy) if self._legacy is not None else None,
        }

    async def stream_turn(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        ctx: TurnContext,
    ) -> DeltaStream:
        """Stream the new turn's reply on the session's thread (or a stateless run)."""
        options = ctx.options
        timeout = build_client_timeout(options)
        system_prompt = rendered_system_prompt(chat_log)
        stateless = bool(options.get(CONF_STATELESS_RUNS, False))

        thread_id: str | None = None
        persist = False
        resume = False
        body: dict[str, Any] = {
            "assistant_id": self._assistant_id,
            "stream_mode": ["messages-tuple", "updates"],
            "on_disconnect": "cancel",
        }
        input_key = str(options.get(CONF_INPUT_MESSAGES_KEY) or DEFAULT_INPUT_MESSAGES_KEY)
        context = self._run_context(chat_log, user_input, None if stateless else system_prompt)
        if stateless:
            url = f"{self._base_url}/runs/stream"
        else:
            scope = str(options.get(CONF_MEMORY_SCOPE, MEMORY_SCOPE_CONVERSATION))
            persist = scope in (MEMORY_SCOPE_DEVICE, MEMORY_SCOPE_AGENT)
            thread_id = await self._thread_id(ctx.memory_key, ctx.legacy_memory_key)
            resume = await self._take_pending_resume(thread_id, timeout)
            url = f"{self._base_url}/threads/{thread_id}/runs/stream"
            strategy = str(options.get(CONF_MULTITASK_STRATEGY) or DEFAULT_MULTITASK_STRATEGY)
            body["if_not_exists"] = "create"
            body["multitask_strategy"] = strategy if strategy in MULTITASK_STRATEGIES else DEFAULT_MULTITASK_STRATEGY
        if resume:
            body["command"] = {"resume": user_input.text}
        else:
            messages: list[dict[str, str]] = []
            if system_prompt and stateless:
                # No accumulated state on a stateless run, so the prompt leads the input.
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_input.text})
            body["input"] = {input_key: messages}
        if context:
            body["context"] = context

        state = _RunState()
        try:
            async for delta in self._run_stream(url, body, options, timeout, chat_log, ctx, state):
                yield delta
        except GeneratorExit, asyncio.CancelledError, TimeoutError, aiohttp.ClientError:
            # Abandoned mid-run (turn cancelled, idle timeout, dropped socket): make sure
            # the server stops too. on_disconnect covers stateless runs (no thread to address).
            if thread_id is not None and state.run_id is not None and not state.finished:
                self._fire_cancel(thread_id, state.run_id)
            raise
        if thread_id is not None and state.interrupted:
            await self._mark_pending_resume(thread_id, persist)

    # --- request assembly ------------------------------------------------------

    @staticmethod
    def _run_context(
        chat_log: conversation.ChatLog, user_input: conversation.ConversationInput, system_prompt: str | None
    ) -> dict[str, str]:
        """Return the run ``context``: the prompt plus HA turn metadata, empty values omitted."""
        values: dict[str, str | None] = {
            "system_prompt": system_prompt,
            "language": user_input.language,
            "device_id": user_input.device_id,
            "satellite_id": user_input.satellite_id,
            "conversation_id": chat_log.conversation_id,
            "user_id": user_input.context.user_id,
        }
        return {key: value for key, value in values.items() if isinstance(value, str) and value}

    # --- threads -----------------------------------------------------------------

    async def _thread_id(self, session_key: str, legacy_key: str | None = None) -> str:
        """Return the legacy mapped thread for ``session_key``, else its deterministic uuid5.

        A legacy entry under ``legacy_key`` (the pre-per-agent device key) moves to
        ``session_key`` on first use, so one agent keeps that thread and siblings get their own.
        """
        async with self._legacy_lock:
            if self._legacy is None:
                self._legacy = await self._store.async_load() or {}
            if session_key not in self._legacy and legacy_key is not None and legacy_key in self._legacy:
                self._legacy[session_key] = self._legacy.pop(legacy_key)
                await self._store.async_save(self._legacy)
            legacy = self._legacy.get(session_key)
        if legacy is not None:
            return legacy
        return str(uuid.uuid5(_THREAD_NAMESPACE, f"{self._entry_key}:{session_key}"))

    async def _pending_map(self) -> dict[str, bool]:
        if self._pending is None:
            loaded = await self._pending_store.async_load() or []
            self._pending = dict.fromkeys(loaded, True)
        return self._pending

    async def _mark_pending_resume(self, thread_id: str, persist: bool) -> None:
        """Remember ``thread_id`` is waiting on an interrupt answer (persisted scopes saved)."""
        pending = await self._pending_map()
        pending[thread_id] = persist
        if persist:
            await self._save_pending(pending)

    async def _take_pending_resume(self, thread_id: str, timeout: aiohttp.ClientTimeout) -> bool:
        """Consume a pending-resume mark; True only if the server still holds an interrupt.

        A ``command.resume`` on a thread with no pending interrupt silently does nothing
        (the user's utterance would be lost), so the mark is confirmed with
        ``GET /threads/{id}``. Its ``interrupts`` field — not ``status``, which a cancelled
        run also leaves as ``interrupted`` — is the signal.
        """
        pending = await self._pending_map()
        if thread_id not in pending:
            return False
        if pending.pop(thread_id):
            await self._save_pending(pending)
        async with self.session.get(
            f"{self._base_url}/threads/{thread_id}", headers=self._request_headers, timeout=timeout
        ) as resp:
            if resp.status == 404:
                return False
            if resp.status >= 400:
                raise BackendStreamError(f"LangGraph thread lookup returned HTTP {resp.status}")
            thread: object = await resp.json()
        interrupts = (json_object(thread) or {}).get("interrupts")
        return bool(interrupts)

    async def _save_pending(self, pending: dict[str, bool]) -> None:
        await self._pending_store.async_save([thread_id for thread_id, persist in pending.items() if persist])

    # --- streaming ---------------------------------------------------------------

    async def _run_stream(
        self,
        url: str,
        body: dict[str, Any],
        options: Mapping[str, Any],
        timeout: aiohttp.ClientTimeout,
        chat_log: conversation.ChatLog,
        ctx: TurnContext,
        state: _RunState,
    ) -> DeltaStream:
        """POST the run and yield role-first deltas: spoken text plus external tool activity.

        A run that used tools but spoke nothing raises after the stream: left alone the chat
        log would end on a tool result, which re-enters the entity's tool loop (re-sending
        the turn); raising hands it to the entity guard, which appends the fallback reply.
        """
        node_filter = str(options.get(CONF_RESPONSE_NODE_FILTER, "") or "")
        messages_key = str(options.get(CONF_INPUT_MESSAGES_KEY) or DEFAULT_INPUT_MESSAGES_KEY)
        block_open = False  # an assistant block that may take more content is open
        needs_reply = False  # tool activity happened since the last spoken text
        input_tokens = output_tokens = 0
        has_usage = False

        async with self._post_checked(
            url,
            label="LangGraph run",
            data=json.dumps(body, default=str),
            headers=self._request_headers,
            timeout=timeout,
        ) as resp:
            async for event in async_iter_sse(resp.content.iter_any()):
                if event.event == _EVENT_ERROR:
                    raise BackendStreamError(f"LangGraph error event: {event.data[:200]}")
                if event.event == _EVENT_METADATA:
                    metadata = _loads(event.data)
                    run_id = (json_object(metadata) or {}).get("run_id")
                    if isinstance(run_id, str):
                        state.run_id = self.last_run_id = run_id
                    continue
                if event.event == _EVENT_UPDATES:
                    update = _parse_updates_frame(event.data, messages_key)
                    if update.has_usage:
                        input_tokens += update.input_tokens
                        output_tokens += update.output_tokens
                        has_usage = True
                    for call_id, name, args in update.tool_calls:
                        yield external_tool_call_delta(call_id, name, args)
                        block_open, needs_reply = False, True
                    for call_id, name, result, error in update.tool_results:
                        yield external_tool_result_delta(call_id, name, result, error=error)
                        block_open, needs_reply = False, True
                    if update.interrupted:
                        state.interrupted = ctx.continue_conversation = True
                        if not block_open:
                            yield {"role": "assistant"}
                        yield {"content": _interrupt_text(update.interrupt)}
                        block_open, needs_reply = True, False
                    continue
                if not (event.event == _EVENT_MESSAGES or event.event.startswith(f"{_EVENT_MESSAGES}/")):
                    continue
                text, node = _parse_messages_frame(event.data)
                if text is None or (node_filter and node != node_filter):
                    continue
                if not block_open:
                    yield {"role": "assistant"}
                    block_open = True
                yield {"content": text}
                needs_reply = False
            state.finished = True

        if has_usage:
            trace_token_stats(chat_log, input_tokens=input_tokens, output_tokens=output_tokens)
        if needs_reply:
            raise BackendStreamError("LangGraph run used tools but produced no reply text")

    # --- best-effort cancel --------------------------------------------------------

    def _fire_cancel(self, thread_id: str, run_id: str) -> None:
        """Schedule a fire-and-forget cancel of ``run_id`` (never blocks the cancelled turn)."""
        self.hass.async_create_background_task(
            self._cancel_run(thread_id, run_id), name=f"{DOMAIN}.langgraph.cancel.{run_id}"
        )

    async def _cancel_run(self, thread_id: str, run_id: str) -> None:
        """POST ``/threads/{id}/runs/{run_id}/cancel``; failures are logged, never raised.

        404 is the normal outcome when ``on_disconnect`` already stopped the run.
        """
        url = f"{self._base_url}/threads/{thread_id}/runs/{run_id}/cancel"
        try:
            async with self.session.post(
                url, headers=self._request_headers, timeout=aiohttp.ClientTimeout(total=_CANCEL_TIMEOUT)
            ) as resp:
                _LOGGER.debug("LangGraph cancel of run %s returned HTTP %s", run_id, resp.status)
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("LangGraph cancel of run %s failed: %s", run_id, err)


def _loads(data: str) -> object:
    """Parse JSON, returning ``None`` for a malformed payload."""
    try:
        return json.loads(data)
    except json.JSONDecodeError, ValueError:
        return None
