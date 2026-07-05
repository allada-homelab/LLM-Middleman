"""LangGraph Platform backend adapter (LLMM-011).

Stateful-thread preset: the graph owns its history and tools server-side, so this
adapter sends only the **new** turn keyed by ``ctx.memory_key`` (mapped to a LangGraph
``thread_id``) and streams the reply back. Text-only passthrough
(``supports_ha_tools = False``); the same wire API serves ``langgraph dev``,
self-hosted, and cloud deployments.

Streaming uses ``stream_mode=messages-tuple`` over SSE (framed by :mod:`._sse`): each
token frame carries a ``[message_chunk, metadata]`` JSON array; token text is read from
the chunk's ``content`` and the emitting node from ``metadata.langgraph_node``. A
configured ``response_node_filter`` restricts spoken output to one node. A successful run
terminates by **SSE EOF** — the server simply closes the stream; there is no terminal
``event: end`` frame. An ``event: error`` frame raises (-> entity guard fallback).

Thread continuity: ``session_key -> thread_id`` is held in-memory for ``conversation``
scope (HA rotates ``conversation_id`` after its 5-minute TTL, so a stale key just makes a
new thread) and persisted via ``helpers.storage.Store`` for ``device``/``agent`` scope so
long-lived threads survive HA restarts. A thread the server has GC'd (404) is transparently
re-created.

.. note::
   The ``messages-tuple`` frame shape and stream-termination behaviour were confirmed
   against a live self-hosted ``langgraph-api`` 0.10.0 capture (LLMM-018 E2E): a
   successful run emits ``event: metadata`` then ``event: messages`` frames and closes on
   EOF — no ``event: end`` is ever sent. ``event: error`` remains the failure signal.
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
    BACKEND_LANGGRAPH,
    CONF_ASSISTANT_ID,
    CONF_INPUT_MESSAGES_KEY,
    CONF_RESPONSE_NODE_FILTER,
    CONF_STATELESS_RUNS,
    CONF_SYSTEM_PROMPT,
    DEFAULT_ASSISTANT_ID,
    DEFAULT_INPUT_MESSAGES_KEY,
    DEFAULT_TIMEOUT,
    DOMAIN,
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

# Foundation connection/option keys. LLMM-005/006 promote these to const.py CONF_*
# symbols; used as literals here (mirroring tests/conftest.py) until they land.
_CONF_BASE_URL = "base_url"
_CONF_API_KEY = "api_key"
_CONF_ENTRY_ID = "entry_id"
_CONF_MEMORY_SCOPE = "memory_scope"
_SCOPE_CONVERSATION = "conversation"
_SCOPE_DEVICE = "device"
_SCOPE_AGENT = "agent"

# Wire event names (confirmed against a live langgraph-api 0.10.0 capture — see module
# note). Success terminates by SSE EOF, so there is no terminal event to match here.
_EVENT_ERROR = "error"
_EVENT_MESSAGES = "messages"


class _ThreadNotFound(Exception):
    """Internal signal: the mapped ``thread_id`` was rejected (404) — recreate it."""


def _headers(api_key: str | None) -> dict[str, str]:
    """Build request headers; attach ``x-api-key`` only when a key is configured."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _extract_text(content: object) -> str:
    """Pull plain text from a LangChain message chunk's ``content``.

    ``content`` is a string for text models, or a list of content blocks (str or
    ``{"type": "text", "text": ...}``) for block-based models. Non-text blocks are
    skipped so only speakable text reaches TTS.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in cast("list[object]", content):
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                mapping = cast("dict[str, object]", block)
                text = mapping.get("text")
                if mapping.get("type") == "text" and isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _parse_messages_frame(data: str) -> tuple[str | None, str | None]:
    """Parse a ``messages-tuple`` SSE ``data`` payload into ``(text, node)``.

    Returns ``(None, None)`` for malformed JSON or an unexpected shape so a single bad
    frame is skipped rather than aborting the stream. Empty token text is normalized to
    ``None`` (no spurious empty delta).
    """
    try:
        parsed: object = json.loads(data)
    except json.JSONDecodeError, ValueError:
        _LOGGER.debug("Skipping malformed LangGraph messages frame")
        return None, None
    if not isinstance(parsed, list):
        return None, None
    frame = cast("list[object]", parsed)
    if len(frame) != 2:
        return None, None
    chunk, metadata = frame[0], frame[1]
    if not isinstance(chunk, dict):
        return None, None
    text = _extract_text(cast("dict[str, object]", chunk).get("content"))
    node = cast("dict[str, object]", metadata).get("langgraph_node") if isinstance(metadata, dict) else None
    return (text or None, node if isinstance(node, str) else None)


class LangGraphAdapter(BackendAdapter):
    """LangGraph Platform preset: stateful threads, ``messages-tuple`` streaming."""

    backend_type: ClassVar[str] = BACKEND_LANGGRAPH
    supports_ha_tools: ClassVar[bool] = False  # tools live server-side in the graph
    supports_memory_scope: ClassVar[bool] = True  # stateful -> key maps to a thread_id

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        connection_data: Mapping[str, Any],
    ) -> None:
        """Store connection state and build the per-entry thread-map store."""
        super().__init__(hass, session, connection_data)
        self._base_url = str(connection_data.get(_CONF_BASE_URL, "")).rstrip("/")
        self._api_key: str | None = connection_data.get(_CONF_API_KEY) or None
        self._assistant_id = str(connection_data.get(CONF_ASSISTANT_ID, DEFAULT_ASSISTANT_ID))
        # Per-entry discriminator for the Store filename (entry_id preferred; base_url
        # slug is a collision-safe fallback for the LLMM-005 wiring not landed yet).
        discriminator = connection_data.get(_CONF_ENTRY_ID) or self._base_url or "default"
        store_key = f"{DOMAIN}.langgraph.{slugify(discriminator)}"
        self._store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, store_key)
        # conversation-scope map (in-memory) and lazily loaded device/agent-scope map.
        self._mem: dict[str, str] = {}
        self._persisted: dict[str, str] | None = None
        self._lock = asyncio.Lock()

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        """Probe ``GET /ok``; on any non-success fall back to ``POST /assistants/search``.

        Raises :class:`BackendAuthError` on 401/403 and :class:`BackendConnectionError`
        on transport failure or a failing fallback, mirroring the ollama config-flow shape.
        """
        session = async_get_clientsession(hass)
        base_url = str(data.get(_CONF_BASE_URL, "")).rstrip("/")
        headers = _headers(data.get(_CONF_API_KEY) or None)
        timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
        try:
            async with session.get(f"{base_url}/ok", headers=headers, timeout=timeout) as resp:
                if resp.status in (401, 403):
                    raise BackendAuthError("LangGraph rejected the API key")
                if resp.status < 400:
                    return
            # /ok unavailable on this deployment — try the assistants search endpoint.
            async with session.post(
                f"{base_url}/assistants/search", data=json.dumps({}), headers=headers, timeout=timeout
            ) as resp:
                if resp.status in (401, 403):
                    raise BackendAuthError("LangGraph rejected the API key")
                if resp.status >= 400:
                    raise BackendConnectionError(f"LangGraph probe failed: HTTP {resp.status}")
        except BackendConnectionError:
            raise
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BackendConnectionError(f"Cannot reach LangGraph at {base_url}") from err

    async def stream_turn(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        ctx: TurnContext,
    ) -> DeltaStream:
        """Ensure a thread for ``ctx.memory_key`` and stream the new turn's reply.

        ``chat_log`` is intentionally unused: LangGraph owns history server-side, so only
        the new turn is sent (stateful contract).
        """
        del chat_log  # server-side history; only the new turn is forwarded
        options = ctx.options
        timeout = build_client_timeout(options)

        if bool(options.get(CONF_STATELESS_RUNS, False)):
            async for delta in self._run_stream(None, user_input, options, timeout):
                yield delta
            return

        scope = str(options.get(_CONF_MEMORY_SCOPE, _SCOPE_CONVERSATION))
        persist = scope in (_SCOPE_DEVICE, _SCOPE_AGENT)
        session_key = ctx.memory_key
        thread_id = await self._ensure_thread(session_key, persist, timeout)
        try:
            async for delta in self._run_stream(thread_id, user_input, options, timeout):
                yield delta
        except _ThreadNotFound:
            # Persisted/cached thread was GC'd server-side: recreate and retry once.
            _LOGGER.debug("LangGraph thread %s gone; recreating", thread_id)
            thread_id = await self._create_thread(timeout)
            await self._store_thread(session_key, thread_id, persist)
            async for delta in self._run_stream(thread_id, user_input, options, timeout):
                yield delta

    # --- thread mapping ------------------------------------------------------

    async def _ensure_thread(self, session_key: str, persist: bool, timeout: aiohttp.ClientTimeout) -> str:
        """Return the mapped ``thread_id`` for ``session_key``, creating one if absent."""
        if not persist:
            existing = self._mem.get(session_key)
            if existing is not None:
                return existing
            thread_id = await self._create_thread(timeout)
            self._mem[session_key] = thread_id
            return thread_id
        async with self._lock:
            mapping = await self._persisted_map()
            existing = mapping.get(session_key)
            if existing is not None:
                return existing
            thread_id = await self._create_thread(timeout)
            mapping[session_key] = thread_id
            await self._store.async_save(mapping)
            return thread_id

    async def _store_thread(self, session_key: str, thread_id: str, persist: bool) -> None:
        """Record ``session_key -> thread_id`` in the in-memory or persisted map."""
        if not persist:
            self._mem[session_key] = thread_id
            return
        async with self._lock:
            mapping = await self._persisted_map()
            mapping[session_key] = thread_id
            await self._store.async_save(mapping)

    async def _persisted_map(self) -> dict[str, str]:
        """Lazily load the persisted device/agent-scope map (call under ``self._lock``)."""
        if self._persisted is None:
            loaded = await self._store.async_load()
            self._persisted = loaded if loaded is not None else {}
        return self._persisted

    async def _create_thread(self, timeout: aiohttp.ClientTimeout) -> str:
        """Create a LangGraph thread and return its id."""
        async with self.session.post(
            f"{self._base_url}/threads", data=json.dumps({}), headers=_headers(self._api_key), timeout=timeout
        ) as resp:
            if resp.status >= 400:
                raise BackendStreamError(f"LangGraph thread creation failed: HTTP {resp.status}")
            payload: object = await resp.json()
        thread_id = cast("dict[str, object]", payload).get("thread_id") if isinstance(payload, dict) else None
        if not isinstance(thread_id, str):
            raise BackendStreamError("LangGraph thread creation returned no thread_id")
        return thread_id

    # --- streaming -----------------------------------------------------------

    def _build_body(self, user_input: conversation.ConversationInput, options: Mapping[str, Any]) -> dict[str, Any]:
        """Assemble the run request body carrying only the new turn."""
        messages: list[dict[str, str]] = []
        system_prompt = str(options.get(CONF_SYSTEM_PROMPT, "") or "")
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_input.text})
        input_key = str(options.get(CONF_INPUT_MESSAGES_KEY, DEFAULT_INPUT_MESSAGES_KEY))
        return {
            "assistant_id": self._assistant_id,
            "input": {input_key: messages},
            "stream_mode": "messages-tuple",
        }

    async def _run_stream(
        self,
        thread_id: str | None,
        user_input: conversation.ConversationInput,
        options: Mapping[str, Any],
        timeout: aiohttp.ClientTimeout,
    ) -> DeltaStream:
        """POST a (threaded or stateless) run and yield role-first token deltas.

        Raises :class:`_ThreadNotFound` on a 404 for a threaded run (before any delta) so
        the caller can recreate the thread; other HTTP/stream failures raise
        :class:`BackendStreamError` for the entity guard.
        """
        if thread_id is None:
            url = f"{self._base_url}/runs/stream"
        else:
            url = f"{self._base_url}/threads/{thread_id}/runs/stream"
        body = json.dumps(self._build_body(user_input, options), default=str)
        node_filter = str(options.get(CONF_RESPONSE_NODE_FILTER, "") or "")

        async with self.session.post(url, data=body, headers=_headers(self._api_key), timeout=timeout) as resp:
            if resp.status == 404 and thread_id is not None:
                raise _ThreadNotFound
            if resp.status >= 400:
                raise BackendStreamError(f"LangGraph run failed: HTTP {resp.status}")
            role_sent = False
            async for event in async_iter_sse(resp.content.iter_any()):
                # Success terminates by SSE EOF (loop exhaustion), not a terminal event.
                if event.event == _EVENT_ERROR:
                    raise BackendStreamError(f"LangGraph error event: {event.data[:200]}")
                if not (event.event == _EVENT_MESSAGES or event.event.startswith(f"{_EVENT_MESSAGES}/")):
                    continue
                text, node = _parse_messages_frame(event.data)
                if text is None:
                    continue
                if node_filter and node != node_filter:
                    continue
                if not role_sent:
                    yield {"role": "assistant"}
                    role_sent = True
                yield {"content": text}
