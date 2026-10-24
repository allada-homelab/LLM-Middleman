"""Backend adapter interface and shared types.

One backend-agnostic ``ConversationEntity`` (LLMM-005) drives any adapter through
the fixed contract defined here; the config flow (LLMM-006/007) builds its
backend-type dropdown and model list from the same surface. Every concrete adapter
(LLMM-008+) subclasses :class:`BackendAdapter`.

Import direction is one-way: ``base`` imports only ``_sse`` and Home Assistant.
Concrete adapters import ``base``; ``backends/__init__.py`` imports the adapters and
assembles the ``BACKEND_TO_CLS`` factory. ``BackendStreamError`` is defined in
``_sse`` (LLMM-002) and re-exported here so adapters get every backend exception
from ``base``.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, cast

import aiohttp
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.llm_middleman.const import (
    CONF_TIMEOUT,
    DEFAULT_TIMEOUT,
    IDLE_TIMEOUT,
)

from ._sse import BackendStreamError

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "BackendAdapter",
    "BackendAuthError",
    "BackendConnectionError",
    "BackendStatusError",
    "BackendStreamError",
    "DeltaStream",
    "TurnContext",
    "bearer_headers",
    "build_client_timeout",
    "loads_json_object",
    "probe_json",
    "rendered_system_prompt",
    "trace_token_stats",
]


def build_client_timeout(options: Mapping[str, Any], default: float = DEFAULT_TIMEOUT) -> aiohttp.ClientTimeout:
    """Return the aiohttp timeout every adapter uses for its streaming POST.

    There is deliberately no ``total`` deadline: aiohttp's ``total`` bounds the whole
    request *including* the streamed body, so it would cut off a reply that is still
    producing tokens (the v0 60 s bug). Instead ``sock_connect`` is the per-agent
    ``CONF_TIMEOUT`` (how long to wait to reach the backend) and ``sock_read`` is the
    idle deadline (:data:`IDLE_TIMEOUT`), which fails a stalled backend fast while a
    live stream runs as long as frames keep arriving. Lives on ``base`` — not the
    entity module — so adapters import it without a cycle back through ``conversation``.
    """
    return aiohttp.ClientTimeout(
        total=None,
        sock_connect=options.get(CONF_TIMEOUT, default),
        sock_read=IDLE_TIMEOUT,
    )


def rendered_system_prompt(chat_log: conversation.ChatLog) -> str | None:
    """Return the agent's rendered system prompt from ``chat_log``, or ``None`` if empty.

    The entity's ``async_provide_llm_data`` renders the subentry ``CONF_PROMPT`` template
    (plus HA's LLM-API/date-time/extra prompts) into ``chat_log.content[0]`` before any
    adapter runs, so stateful adapters forward this rather than the raw option template.
    """
    first = chat_log.content[0] if chat_log.content else None
    if isinstance(first, conversation.SystemContent) and first.content:
        return first.content
    return None


def trace_token_stats(
    chat_log: conversation.ChatLog,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int | None = None,
) -> None:
    """Record one round-trip's token usage on the conversation trace.

    Same ``{"stats": {...}}`` agent-detail shape HA core's anthropic/openai/cloud
    entities trace; ``cached_input_tokens`` only when the backend reports a cache.
    """
    stats: dict[str, int] = {"input_tokens": input_tokens}
    if cached_input_tokens is not None:
        stats["cached_input_tokens"] = cached_input_tokens
    stats["output_tokens"] = output_tokens
    chat_log.async_trace({"stats": stats})


def bearer_headers(token: str | None) -> dict[str, str]:
    """Return an ``Authorization: Bearer`` header when ``token`` is set, else empty."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def loads_json_object(raw: str | bytes, *, warn_label: str | None = None) -> dict[str, Any] | None:
    """Parse ``raw`` as a JSON object; ``None`` for malformed JSON or a non-object value.

    With ``warn_label`` set, a parse failure is logged at WARNING (first 200 chars of the
    payload) so a preset can surface a bad frame without aborting the stream.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        if warn_label is not None:
            _LOGGER.warning("Failed to parse %s JSON payload: %s", warn_label, raw[:200])
        return None
    if isinstance(parsed, dict):
        return cast("dict[str, Any]", parsed)
    return None


# Canonical HA delta-dict stream every adapter yields. Single-arg AsyncGenerator:
# the entity only iterates (never sends values in), so the send type defaults to None.
type DeltaStream = AsyncGenerator[conversation.AssistantContentDeltaDict]

# Pre-check hook for an error status: may raise a preset-specific error, or return to
# fall through to the generic log-and-raise (see ``BackendAdapter._post_checked``).
type ErrorStatusHook = Callable[[aiohttp.ClientResponse], Awaitable[None]]


class BackendConnectionError(HomeAssistantError):
    """A backend connection probe failed (config flow maps to ``cannot_connect``)."""


class BackendAuthError(BackendConnectionError):
    """A backend rejected credentials (config flow maps to ``invalid_auth``)."""


class BackendStatusError(BackendConnectionError):
    """A probe got a non-auth HTTP error status (distinct from a transport failure)."""


async def probe_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    label: str,
    headers: Mapping[str, str],
    method: Literal["GET", "POST"] = "GET",
    data: str | None = None,
    parse: bool = True,
) -> dict[str, Any]:
    """Run a config-flow probe request and return its JSON-object body.

    401/403 -> :class:`BackendAuthError`; any other status >= 400 ->
    :class:`BackendStatusError`; a transport failure/timeout, an unparsable body, or a
    non-object JSON body -> :class:`BackendConnectionError`. With ``parse=False`` only the
    status is checked and ``{}`` is returned (for endpoints whose body is not an object).
    ``session`` is passed in so each adapter keeps its own ``async_get_clientsession``.
    """
    timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
    try:
        request = (
            session.post(url, data=data, headers=headers, timeout=timeout)
            if method == "POST"
            else session.get(url, headers=headers, timeout=timeout)
        )
        async with request as response:
            if response.status in (401, 403):
                raise BackendAuthError(f"{label} rejected credentials (HTTP {response.status})")
            if response.status >= 400:
                raise BackendStatusError(f"{label} probe returned HTTP {response.status}")
            if not parse:
                return {}
            try:
                payload: object = await response.json()
            except ValueError as err:
                raise BackendConnectionError(f"{label} probe returned invalid JSON") from err
    except (TimeoutError, aiohttp.ClientError) as err:
        raise BackendConnectionError(f"Cannot reach {label} at {url}: {err}") from err
    if not isinstance(payload, dict):
        raise BackendConnectionError(f"{label} probe returned a non-object JSON body")
    return cast("dict[str, Any]", payload)


@dataclass
class TurnContext:
    """Per-turn channel between the entity and an adapter.

    Created fresh each turn by the entity and never stored on the adapter: the
    adapter instance lives in ``entry.runtime_data``, shared across subentries and
    concurrent turns, so per-turn mutable state on the adapter would race. Stateful
    adapters read :attr:`memory_key`; an adapter signals follow-up listening by
    setting :attr:`continue_conversation`, which the entity ORs into the result.
    """

    options: Mapping[str, Any]  # the agent subentry's options for this turn
    memory_key: str  # session key derived by the entity from the memory scope
    continue_conversation: bool = False  # adapter may set; entity ORs into result
    fallback: bool = False  # entity-owned: the last round-trip ended in the fallback message


class BackendAdapter(ABC):
    """Abstract base for all backend presets.

    Built once by ``__init__.py`` setup via the ``BACKEND_TO_CLS`` factory and
    stored in ``entry.runtime_data``; the shared instance is driven per turn with a
    fresh :class:`TurnContext`.

    Two-axis contract for :meth:`stream_turn`:

    * **Stateless** adapters (openai-compat, ollama) rebuild provider messages from
      ``chat_log.content`` each turn (with ollama-style trim via ``CONF_MAX_HISTORY``)
      and pass HA tool schemas when ``chat_log.llm_api`` is set.
    * **Stateful** adapters (langgraph, converse, n8n) send only the new turn, keyed
      on ``ctx.memory_key`` (e.g. mapped to a LangGraph ``thread_id``); the backend
      owns the history.

    Adapters must not stash per-turn state on ``self`` (see :class:`TurnContext`).
    """

    # Factory key and config-flow dropdown value. Set by each concrete subclass.
    backend_type: ClassVar[str]
    # Gates CONF_LLM_HASS_API in the subentry flow; tool-capable backends set True.
    supports_ha_tools: ClassVar[bool] = False
    # Gates CONF_MEMORY_SCOPE in the subentry flow; stateful backends set True.
    supports_memory_scope: ClassVar[bool] = False
    # Prefix the forwarded turn text with the local date/time. Adapters that replay the
    # chat log or forward the rendered system prompt (which already carries HA's date/time
    # prompt) opt out; the default keeps temporal context for text-only presets.
    wants_time_prefix: ClassVar[bool] = True

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        connection_data: Mapping[str, Any],
    ) -> None:
        """Store the HA instance, shared client session, and parent-entry data.

        Adapters read connection state (``base_url`` / ``api_key`` / token) from
        ``self.connection_data``. LLMM-005's ``async_setup_entry`` constructs the
        adapter as ``adapter_cls(hass, async_create_clientsession(hass), entry.data)``.
        """
        self.hass = hass
        self.session = session
        self.connection_data = connection_data

    @asynccontextmanager
    async def _post_checked(
        self,
        url: str,
        *,
        label: str,
        headers: Mapping[str, str],
        timeout: aiohttp.ClientTimeout,
        json: Any = None,
        data: str | None = None,
        error_cls: type[Exception] = BackendStreamError,
        on_error_status: ErrorStatusHook | None = None,
    ) -> AsyncGenerator[aiohttp.ClientResponse]:
        """POST a turn request and yield the response once its status is a success.

        On status >= 400, ``on_error_status`` (if given) runs first so a preset can map a
        specific status to its own signal (a stale thread/conversation 404, an auth
        error); if it returns, the body (first 500 chars) is logged at ERROR as
        ``"<label> returned HTTP <status>: <body>"`` and ``error_cls`` is raised, so an
        error status always reaches the entity guard named, never as a silent empty stream.
        """
        async with self.session.post(url, json=json, data=data, headers=headers, timeout=timeout) as response:
            if response.status >= 400:
                if on_error_status is not None:
                    await on_error_status(response)
                detail = (await response.text())[:500]
                _LOGGER.error("%s returned HTTP %s: %s", label, response.status, detail)
                raise error_cls(f"{label} returned HTTP {response.status}")
            yield response

    @classmethod
    @abstractmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        """Probe the backend's real endpoint; return ``None``, raise on failure.

        openai: ``GET /v1/models`` · ollama: ``GET /api/tags`` · langgraph:
        ``GET /ok`` (fallback ``POST /assistants/search``) · converse: transport-level
        check. Raises :class:`BackendConnectionError` / :class:`BackendAuthError`,
        which the config flow maps to ``cannot_connect`` / ``invalid_auth`` — never
        return an error string.
        """

    @classmethod
    async def async_list_models(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> list[str] | None:
        """Model catalog for the subentry model dropdown.

        Returns the model-id list for backends with a catalog (openai from
        ``/v1/models``, ollama from ``/api/tags``) or ``None`` when there is no
        catalog (converse/langgraph/n8n). The base default has no catalog.
        """
        return None

    @abstractmethod
    def stream_turn(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        ctx: TurnContext,
    ) -> DeltaStream:
        """Run one provider round-trip and yield canonical HA delta dicts.

        Declared as ``def`` returning an ``AsyncGenerator``; concrete adapters
        implement it as an ``async def`` generator with ``yield``. The first delta of
        each assistant block must carry ``role`` (HA treats a role-less delta as a
        continuation).
        """
        raise NotImplementedError
