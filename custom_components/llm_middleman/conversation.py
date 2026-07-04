"""Backend-agnostic conversation entity for LLM Middleman.

This is the ONE ``ConversationEntity`` every backend preset shares. It wires HA's
``ChatLog`` to a backend adapter's ``stream_turn``, hardens the "never hang the
pipeline" guarantee behind :meth:`_guarded`, and applies per-agent timeouts,
follow-up listening, and memory-scope session-key derivation. All provider-specific
streaming lives in the adapters (``backends/``), never here.

One entity is created per ``conversation`` subentry (core openai/ollama pattern); the
shared adapter lives in ``entry.runtime_data``. The single-turn drive is factored into
:meth:`_async_run_turn` so LLMM-014 can wrap it in the HA tool loop; this ticket drives
``stream_turn`` exactly once (text-only, ``llm_api=None``).
"""

from __future__ import annotations

import logging
from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_PROMPT, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LLMMiddlemanConfigEntry
from .backends.base import BackendAdapter, DeltaStream, TurnContext
from .const import (
    CONF_MEMORY_SCOPE,
    DOMAIN,
    ERROR_MESSAGE,
    MEMORY_SCOPE_AGENT,
    MEMORY_SCOPE_CONVERSATION,
    MEMORY_SCOPE_DEVICE,
    SUBENTRY_TYPE_CONVERSATION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LLMMiddlemanConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one conversation entity per ``conversation`` subentry."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_CONVERSATION:
            continue
        async_add_entities(
            [LLMMiddlemanConversationEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class LLMMiddlemanConversationEntity(
    conversation.ConversationEntity,
    # AbstractConversationAgent is imported into the conversation package namespace
    # but omitted from its __all__, so basedpyright strict flags it; core
    # openai_conversation subclasses it the same way. Mirrors the core template:
    # both the base and the async_set_agent lifecycle below are kept per that
    # pattern (LLMM-005 agent-registration checkpoint — verified against the
    # installed HA source, which still calls async_set_agent).
    conversation.AbstractConversationAgent,  # pyright: ignore[reportPrivateImportUsage]
):
    """A passthrough conversation agent that forwards turns to a backend adapter."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = True

    def __init__(self, entry: LLMMiddlemanConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity for one conversation subentry."""
        self.entry = entry
        self.subentry = subentry
        self.adapter: BackendAdapter = entry.runtime_data
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="LLM Middleman",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return supported languages — the backend handles any language."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """Register as a conversation agent when added to HA."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister as a conversation agent when removed from HA."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Forward the turn to the backend and stream the reply back."""
        options = self.subentry.data

        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                # No HA LLM API until LLMM-014 wires CONF_LLM_HASS_API + the tool
                # loop; this ticket is text-only passthrough.
                None,
                options.get(CONF_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        ctx = await self._async_run_turn(user_input, chat_log)

        result = conversation.async_get_result_from_chat_log(user_input, chat_log)
        # Follow-up listening: HA already ORs in "reply ends with '?'"; the adapter
        # may explicitly request it via the per-turn TurnContext.
        result.continue_conversation = result.continue_conversation or ctx.continue_conversation
        return result

    async def _async_run_turn(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> TurnContext:
        """Drive ONE adapter round-trip through the guard into the chat log.

        Factored out (single turn, no tool loop) so LLMM-014 can wrap it in
        ``for _ in range(MAX_TOOL_ITERATIONS)`` while ``chat_log.unresponded_tool_results``.
        Returns the per-turn :class:`TurnContext` (created fresh here, never on the
        shared adapter) so the caller can read ``continue_conversation``.
        """
        ctx = TurnContext(
            options=self.subentry.data,
            memory_key=self._derive_memory_key(user_input, chat_log),
        )
        async for _content in chat_log.async_add_delta_content_stream(
            self.entity_id,
            self._guarded(self.adapter.stream_turn(chat_log, user_input, ctx)),
        ):
            pass
        return ctx

    async def _guarded(self, stream: DeltaStream) -> DeltaStream:
        """Never-hangs wrapper around an adapter stream.

        Guarantees at least one ``AssistantContent`` on every exit path so
        ``async_get_result_from_chat_log`` never raises and the pipeline never hangs:

        * Role-first invariant — if the first yielded delta has no ``role`` key, a
          leading ``{"role": "assistant"}`` is injected (HA rejects a first delta
          without a role).
        * Deltas pass through untrimmed (empty-string ``content`` included).
        * ``except Exception`` broadly (the v0 holes were ``ValueError`` from aiohttp's
          64 KB readline limit and ``UnicodeDecodeError``, plus ``TimeoutError``);
          logs with ``_LOGGER.exception`` and appends the fallback message, opening a
          role first only if nothing was emitted yet.
        * A silent end (stream yields nothing) emits role + fallback.
        """
        started = False
        try:
            async for delta in stream:
                if not started and "role" not in delta:
                    yield {"role": "assistant"}
                started = True
                yield delta
        except Exception:
            _LOGGER.exception("Backend stream failed; returning fallback message")
            if not started:
                yield {"role": "assistant"}
            yield {"content": ERROR_MESSAGE}
            return

        if not started:
            yield {"role": "assistant"}
            yield {"content": ERROR_MESSAGE}

    def _derive_memory_key(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> str:
        """Derive the session key stateful adapters use, per ``CONF_MEMORY_SCOPE``.

        ``conversation`` (default) → ``conversation_id``; ``device`` → the device id
        (falling back to ``conversation_id`` when the turn has no device); ``agent`` →
        the subentry id (one global thread per agent). Stateless adapters ignore it.
        """
        scope = self.subentry.data.get(CONF_MEMORY_SCOPE, MEMORY_SCOPE_CONVERSATION)
        if scope == MEMORY_SCOPE_AGENT:
            return self.subentry.subentry_id
        if scope == MEMORY_SCOPE_DEVICE and user_input.device_id:
            return user_input.device_id
        return chat_log.conversation_id
