"""Dify backend adapter (LLMM-020).

First-class preset for Dify (dify.ai / self-hosted ``langgenius/dify``). Dify exposes
one chat API for its Chatbot, Agent, and Chatflow app types:
``POST {base}/chat-messages`` with ``response_mode: "streaming"`` (blocking mode is
**not** supported for Agent apps, so this adapter is streaming-only). Replies stream as
WHATWG SSE (framed by :mod:`._sse`) with named ``event:`` types: ``agent_message``
carries answer deltas for Agent apps, ``message`` for Chatbot/Chatflow — handling both
makes one preset cover all three app types. ``message_end`` terminates; an in-stream
``event: error`` (HTTP still 200) raises. ``ping``/``agent_thought``/``message_file``/
``message_replace``/``tts_message*`` are ignored (``agent_thought``/``message_replace``
are debug-logged; a moderation ``message_replace`` cannot un-speak already-streamed text,
so it is never applied retroactively).

Dify owns conversation memory server-side: the first turn returns a ``conversation_id``
on every event, and echoing it back continues the conversation → stateful
(``supports_memory_scope = True``). Unlike LangGraph the server id is only known
*mid-stream* (the first event carrying it), so the ``session_key -> conversation_id`` map
is persisted after capture, not before the POST — in-memory for ``conversation`` scope,
via ``helpers.storage.Store`` for ``device``/``agent`` scope. A stale/deleted id (pre-stream
HTTP 404 ``conversation_not_exists``) is dropped and the turn retried once without an id.

Tools live inside the Dify app; the API has no client-tool passthrough
(``supports_ha_tools = False``). The per-agent system prompt is likewise owned by the
Dify app, so ``CONF_PROMPT`` is not forwarded. API reference:
https://docs.dify.ai/api-reference/chats/send-chat-message
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, cast

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from custom_components.llm_middleman.const import (
    BACKEND_DIFY,
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MEMORY_SCOPE,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MEMORY_SCOPE_AGENT,
    MEMORY_SCOPE_CONVERSATION,
    MEMORY_SCOPE_DEVICE,
    STORAGE_VERSION,
)

from ._sse import BackendStreamError, async_iter_sse
from .base import (
    BackendAdapter,
    BackendAuthError,
    BackendConnectionError,
    DeltaStream,
    build_client_timeout,
)

if TYPE_CHECKING:
    from homeassistant.components import conversation

    from .base import TurnContext

_LOGGER = logging.getLogger(__name__)

# Per-entry discriminator key (rides along on connection_data from __init__.py setup).
_CONF_ENTRY_ID = "entry_id"

# Every HA conversation is one Dify end-user; conversation ids are globally unique so
# scopes cannot collide (per-HA-user attribution is a future config option, not this ticket).
_USER = "home-assistant"

# App modes the chat API serves; anything else (workflow/completion) is a misconfiguration.
_CHAT_APP_MODES = frozenset({"chat", "agent-chat", "advanced-chat"})

# Wire event names. ``agent_message`` (Agent apps) and ``message`` (Chatbot/Chatflow)
# both carry answer deltas; the rest are terminal/ignored per the module docstring.
_CONTENT_EVENTS = frozenset({"agent_message", "message"})
_EVENT_MESSAGE_END = "message_end"
_EVENT_ERROR = "error"
_LOGGED_IGNORED_EVENTS = frozenset({"agent_thought", "message_replace"})


class _ConversationNotFound(Exception):
    """Internal signal: the echoed ``conversation_id`` was rejected (404) — recreate it."""


def _parse_data(raw: str) -> dict[str, Any] | None:
    """Parse an SSE/HTTP JSON payload as an object, tolerating malformed input."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _LOGGER.warning("Failed to parse Dify JSON payload: %s", raw[:200])
        return None
    if isinstance(parsed, dict):
        return cast("dict[str, Any]", parsed)
    return None


class DifyAdapter(BackendAdapter):
    """Dify preset: streaming-only chat API, server-side memory, no HA tools."""

    backend_type: ClassVar[str] = BACKEND_DIFY
    supports_ha_tools: ClassVar[bool] = False  # tools live in the Dify app
    supports_memory_scope: ClassVar[bool] = True  # stateful -> key maps to a conversation_id

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
        store_key = f"{DOMAIN}.dify.{slugify(discriminator)}"
        self._store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, store_key)
        # conversation-scope map (in-memory) and lazily loaded device/agent-scope map.
        self._mem: dict[str, str] = {}
        self._persisted: dict[str, str] | None = None
        self._lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        """Build request headers; the Dify app key is a required bearer token."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        """Probe ``GET {base}/info``; reject bad keys and non-chat app types.

        401/403 -> :class:`BackendAuthError`; transport failure or any other non-2xx ->
        :class:`BackendConnectionError`. When the app metadata reports a ``mode`` that is
        not a chat-type app (the entry points at a workflow/completion app) ->
        :class:`BackendConnectionError`.
        """
        session = async_get_clientsession(hass)
        base_url = str(data.get(CONF_BASE_URL, "")).rstrip("/")
        api_key = data.get(CONF_API_KEY) or None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
        try:
            async with session.get(f"{base_url}/info", headers=headers, timeout=timeout) as resp:
                if resp.status in (401, 403):
                    raise BackendAuthError("Dify rejected the API key")
                if resp.status >= 400:
                    raise BackendConnectionError(f"Dify /info returned HTTP {resp.status}")
                payload: object = await resp.json()
        except BackendConnectionError:
            raise
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BackendConnectionError(f"Cannot reach Dify at {base_url}") from err
        mode = cast("dict[str, object]", payload).get("mode") if isinstance(payload, dict) else None
        if isinstance(mode, str) and mode not in _CHAT_APP_MODES:
            raise BackendConnectionError(f"Dify app mode {mode!r} is not a chat app")

    async def stream_turn(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        ctx: TurnContext,
    ) -> DeltaStream:
        """Send the new turn to ``{base}/chat-messages`` and stream its SSE reply.

        ``chat_log`` is unused: Dify owns history server-side, so only the new turn is
        sent, keyed on ``ctx.memory_key`` -> the mapped ``conversation_id``. A stale id
        rejected pre-stream (404 ``conversation_not_exists``) is dropped and the turn is
        retried once without a ``conversation_id``.
        """
        del chat_log  # server-side history; only the new turn is forwarded
        options = ctx.options
        timeout = build_client_timeout(options)
        scope = str(options.get(CONF_MEMORY_SCOPE, MEMORY_SCOPE_CONVERSATION))
        persist = scope in (MEMORY_SCOPE_DEVICE, MEMORY_SCOPE_AGENT)
        session_key = ctx.memory_key

        conversation_id = await self._lookup(session_key, persist)
        # ``task_id`` is captured inside ``_run_stream`` but read here: GeneratorExit is
        # thrown into *this* generator's yield on cancellation, so the best-effort stop
        # must fire from this frame (deterministic, not via the inner generator's GC).
        state: dict[str, str | None] = {"task_id": None}
        try:
            try:
                async for delta in self._run_stream(conversation_id, session_key, persist, user_input, timeout, state):
                    yield delta
            except _ConversationNotFound:
                # Echoed id was GC'd/deleted server-side: drop the mapping and retry once.
                _LOGGER.debug("Dify conversation %s gone; recreating", conversation_id)
                await self._drop(session_key, persist)
                async for delta in self._run_stream(None, session_key, persist, user_input, timeout, state):
                    yield delta
        except GeneratorExit, asyncio.CancelledError:
            task_id = state["task_id"]
            if task_id is not None:
                self._fire_stop(task_id)
            raise

    async def _run_stream(
        self,
        conversation_id: str | None,
        session_key: str,
        persist: bool,
        user_input: conversation.ConversationInput,
        timeout: aiohttp.ClientTimeout,
        state: dict[str, str | None],
    ) -> DeltaStream:
        """POST one turn and yield role-first content deltas from the SSE reply.

        Captures ``conversation_id`` from the first event carrying it and persists it
        (mid-stream, since the id is unknown before the POST), and records the ``task_id``
        into ``state`` so the caller can fire a best-effort stop on cancellation. Raises
        :class:`_ConversationNotFound` on a pre-stream 404 ``conversation_not_exists`` (so
        the caller recreates); :class:`BackendAuthError`/:class:`BackendConnectionError` on
        other pre-stream failures; :class:`BackendStreamError` on an in-stream ``error``
        event.
        """
        body: dict[str, Any] = {
            "query": user_input.text,
            "inputs": {},
            "response_mode": "streaming",
            "user": _USER,
            "auto_generate_name": False,
        }
        if conversation_id is not None:
            body["conversation_id"] = conversation_id

        role_sent = False
        captured = False
        async with self.session.post(
            f"{self._base_url}/chat-messages",
            data=json.dumps(body),
            headers=self._headers(),
            timeout=timeout,
        ) as resp:
            if resp.status == 404 and conversation_id is not None:
                detail = _parse_data(await resp.text())
                if detail is not None and detail.get("code") == "conversation_not_exists":
                    raise _ConversationNotFound
                raise BackendConnectionError(f"Dify chat-messages 404: {detail.get('code') if detail else 'unknown'}")
            if resp.status in (401, 403):
                raise BackendAuthError("Dify rejected the API key")
            if resp.status >= 400:
                detail = _parse_data(await resp.text()) or {}
                _LOGGER.error(
                    "Dify chat-messages HTTP %s: code=%s message=%s",
                    resp.status,
                    detail.get("code"),
                    detail.get("message"),
                )
                raise BackendConnectionError(f"Dify chat-messages returned HTTP {resp.status}")

            async for sse in async_iter_sse(resp.content.iter_any()):
                event = sse.event
                if event == _EVENT_ERROR:
                    detail = _parse_data(sse.data) or {}
                    _LOGGER.error("Dify error event: code=%s message=%s", detail.get("code"), detail.get("message"))
                    raise BackendStreamError(f"Dify error event: {detail.get('code')}")
                if event in _LOGGED_IGNORED_EVENTS:
                    # message_replace (moderation) cannot un-speak streamed text; never applied.
                    _LOGGER.debug("Dify %s event ignored: %s", event, sse.data[:200])
                    continue
                if event not in _CONTENT_EVENTS and event != _EVENT_MESSAGE_END:
                    continue  # ping / message_file / tts_* / unknown
                payload = _parse_data(sse.data)
                if payload is None:
                    continue
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
                if event == _EVENT_MESSAGE_END:
                    return
                answer = payload.get("answer")
                if isinstance(answer, str) and answer:
                    if not role_sent:
                        yield {"role": "assistant"}
                        role_sent = True
                    yield {"content": answer}

    # --- best-effort stop -------------------------------------------------------

    def _fire_stop(self, task_id: str) -> None:
        """Schedule a fire-and-forget ``stop`` for ``task_id`` (mid-stream cancellation)."""
        self.hass.async_create_background_task(self._stop(task_id), name=f"{DOMAIN}.dify.stop.{task_id}")

    async def _stop(self, task_id: str) -> None:
        """POST ``/chat-messages/{task_id}/stop``; swallow errors (best-effort abort)."""
        url = f"{self._base_url}/chat-messages/{task_id}/stop"
        try:
            async with self.session.post(
                url,
                data=json.dumps({"user": _USER}),
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ):
                return
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("Dify stop request failed for task %s: %s", task_id, err)

    # --- conversation mapping ---------------------------------------------------

    async def _lookup(self, session_key: str, persist: bool) -> str | None:
        """Return the mapped ``conversation_id`` for ``session_key``, or ``None``."""
        if not persist:
            return self._mem.get(session_key)
        async with self._lock:
            return (await self._persisted_map()).get(session_key)

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
