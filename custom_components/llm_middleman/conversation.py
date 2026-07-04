"""Conversation entity for LLM Middleman.

The entity forwards each recognized turn to the external agent over the
``/v1/converse`` SSE contract and translates ``text_delta`` events into HA
``AssistantContentDeltaDict`` deltas so TTS can start speaking early. It runs no
LLM and executes no tools of its own — the external agent owns all of that.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any, Literal

import aiohttp
from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LLMMiddlemanConfigEntry
from .const import (
    CONF_SYSTEM_PROMPT,
    CONF_TOKEN,
    CONF_URL,
    CONVERSE_PATH,
    DEFAULT_TIMEOUT,
    DOMAIN,
    ERROR_MESSAGE,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LLMMiddlemanConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the conversation entity for a config entry."""
    async_add_entities([LLMMiddlemanConversationEntity(config_entry)])


class LLMMiddlemanConversationEntity(
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
):
    """A passthrough conversation agent that forwards turns to an external service."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = True

    def __init__(self, entry: LLMMiddlemanConfigEntry) -> None:
        """Initialize the conversation entity."""
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="LLM Middleman",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return supported languages — the external agent handles any language."""
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
        """Forward the turn to the external agent and stream the reply back."""
        # Standard chat-log setup (system prompt). This shim exposes no HA tools,
        # so no LLM API is provided — the external agent owns tool calling.
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                None,
                self.entry.data.get(CONF_SYSTEM_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        await self._async_forward(user_input, chat_log)

        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _async_forward(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> None:
        """POST the turn to the middleman and feed its SSE deltas into the chat log."""
        body: dict[str, Any] = {
            "conversation_id": chat_log.conversation_id,
            "text": user_input.text,
            "language": user_input.language,
        }
        if user_input.device_id:
            body["device_id"] = user_input.device_id

        headers = {"Accept": "text/event-stream"}
        if token := self.entry.data.get(CONF_TOKEN):
            headers["Authorization"] = f"Bearer {token}"

        url = self.entry.data[CONF_URL].rstrip("/") + CONVERSE_PATH

        async for _content in chat_log.async_add_delta_content_stream(
            self.entity_id,
            self._stream_deltas(url, body, headers),
        ):
            pass

    async def _stream_deltas(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        """Translate the middleman's SSE stream into HA assistant deltas.

        Guarantees at least one ``AssistantContent`` is produced (a graceful
        fallback on any error/timeout) so ``async_get_result_from_chat_log`` never
        fails and the Assist pipeline never hangs.
        """
        session: aiohttp.ClientSession = self.entry.runtime_data
        started = False
        try:
            async with session.post(
                url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:500]
                    _LOGGER.error("Middleman returned HTTP %s: %s", response.status, detail)
                    yield {"role": "assistant"}
                    yield {"content": ERROR_MESSAGE}
                    return

                event: str | None = None
                async for raw in response.content:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if not line:
                        event = None  # blank line terminates an SSE event
                        continue
                    if line.startswith(":"):
                        continue  # SSE comment / keep-alive
                    if line.startswith("event:"):
                        event = line[len("event:") :].strip()
                        continue
                    if not line.startswith("data:"):
                        continue

                    payload = _parse_data(line[len("data:") :].strip())
                    if payload is None:
                        continue

                    if event == "text_delta":
                        if delta := payload.get("delta"):
                            if not started:
                                yield {"role": "assistant"}
                                started = True
                            yield {"content": delta}
                    elif event == "error":
                        _LOGGER.error(
                            "Middleman error event: %s — %s",
                            payload.get("code"),
                            payload.get("message"),
                        )
                        if not started:
                            yield {"role": "assistant"}
                            started = True
                        yield {"content": ERROR_MESSAGE}
                        return
                    elif event == "done":
                        # A stream with no text_delta still needs a final message.
                        if not started:
                            yield {"role": "assistant"}
                            yield {"content": payload.get("text") or ERROR_MESSAGE}
                            started = True
                        return
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Middleman request failed: %s", err)
            if not started:
                yield {"role": "assistant"}
                yield {"content": ERROR_MESSAGE}
            return

        # Stream ended without a done/error event and produced nothing usable.
        if not started:
            yield {"role": "assistant"}
            yield {"content": ERROR_MESSAGE}


def _parse_data(data: str) -> dict[str, Any] | None:
    """Parse an SSE ``data:`` payload as JSON, tolerating malformed lines."""
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        _LOGGER.warning("Failed to parse SSE data payload: %s", data)
        return None
    return parsed if isinstance(parsed, dict) else None
