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

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

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

__all__ = [
    "BackendAdapter",
    "BackendAuthError",
    "BackendConnectionError",
    "BackendStreamError",
    "DeltaStream",
    "TurnContext",
    "build_client_timeout",
]


def build_client_timeout(options: Mapping[str, Any]) -> aiohttp.ClientTimeout:
    """Return the aiohttp timeout every adapter uses for its streaming POST.

    ``total`` is the per-agent ``CONF_TIMEOUT`` (default 60 s); ``sock_read`` is the
    idle deadline (:data:`IDLE_TIMEOUT`), so a responsive-but-slow stream isn't killed
    by a single total deadline (the v0 60 s bug). Lives on ``base`` — not the entity
    module — so adapters import it without a cycle back through ``conversation``.
    """
    return aiohttp.ClientTimeout(
        total=options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
        sock_read=IDLE_TIMEOUT,
    )


# Canonical HA delta-dict stream every adapter yields. Single-arg AsyncGenerator:
# the entity only iterates (never sends values in), so the send type defaults to None.
type DeltaStream = AsyncGenerator[conversation.AssistantContentDeltaDict]


class BackendConnectionError(HomeAssistantError):
    """A backend connection probe failed (config flow maps to ``cannot_connect``)."""


class BackendAuthError(BackendConnectionError):
    """A backend rejected credentials (config flow maps to ``invalid_auth``)."""


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
