"""Custom ``/v1/converse`` SSE adapter — the reference backend preset.

This is the canonical, cleanest-fit adapter for a text-only voice turn: text in,
streaming text out, no tool plumbing. It defines the **internal canonical delta
shape** every other preset normalizes into — the first delta of an assistant block
carries ``{"role": "assistant"}`` (HA treats a role-less delta as a continuation),
then each subsequent delta carries ``{"content": <chunk>}``.

It ports the v0 shim's bespoke ``/v1/converse`` contract (``text_delta`` / ``done``
/ ``error`` events, see ``docs/knowledge/03`` §4) but replaces v0's two defects:

* the byte stream is framed by the spec-compliant :func:`async_iter_sse` reader
  (LLMM-002), never v0's per-line ``response.content`` loop; and
* transport failures propagate as exceptions for the entity's never-hangs guard
  (LLMM-005) to convert into the fallback, never v0's narrow inline ``except``.

It is **stateful**: the backend owns conversation history, keyed on the forwarded
session key (``ctx.memory_key`` → the request's ``conversation_id`` field), so no
history is replayed. It finally wires ``done.continue_conversation`` — the
documented follow-up-listening field v0 dropped — by setting
``ctx.continue_conversation``, which the entity ORs into its ``ConversationResult``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, cast

import aiohttp
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.llm_middleman.const import (
    BACKEND_CONVERSE,
    CONF_BASE_URL,
    CONF_TOKEN,
    CONVERSE_PATH,
    DEFAULT_TIMEOUT,
    ERROR_MESSAGE,
)

from ._sse import BackendStreamError, async_iter_sse
from .base import BackendAdapter, BackendConnectionError, DeltaStream, TurnContext

_LOGGER = logging.getLogger(__name__)


class ConverseAdapter(BackendAdapter):
    """Reference adapter for the custom ``/v1/converse`` SSE preset.

    Stateful (the backend owns history keyed on ``ctx.memory_key``); exposes no HA
    tools (the backend owns its own tool calling server-side).
    """

    backend_type = BACKEND_CONVERSE
    supports_ha_tools = False
    supports_memory_scope = True  # stateful: backend partitions history by memory_key

    @classmethod
    async def async_validate_connection(cls, hass: HomeAssistant, data: Mapping[str, Any]) -> None:
        """Transport-level reachability probe: converse exposes no health/catalog endpoint.

        Any HTTP response from the base URL proves the transport is reachable, so we
        don't interpret the status code — only genuine connection/timeout failures
        map to :class:`BackendConnectionError` (the config flow's ``cannot_connect``).
        """
        session = async_get_clientsession(hass)
        headers: dict[str, str] = {}
        if token := data.get(CONF_TOKEN):
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with session.get(
                data[CONF_BASE_URL],
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ):
                return
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BackendConnectionError(f"Cannot reach converse backend: {err}") from err

    async def stream_turn(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        ctx: TurnContext,
    ) -> DeltaStream:
        """POST the new turn to ``{base_url}/v1/converse`` and stream its SSE reply.

        Sends only the current turn plus ``ctx.memory_key`` as ``conversation_id``
        (stateful — the backend owns history). Dispatches on the named events:
        ``text_delta`` → canonical role-first content deltas; ``done`` → terminate
        (deltas are authoritative; ``done.text`` fills in only when nothing streamed)
        and honor ``done.continue_conversation``; ``error`` / non-200 → raise
        :class:`BackendStreamError` for the entity guard to turn into the fallback.

        On a silent stream end (no ``done``/``error``) it simply returns; the guard
        supplies the fallback so the pipeline never hangs.
        """
        body: dict[str, Any] = {
            "conversation_id": ctx.memory_key,
            "text": user_input.text,
            "language": user_input.language,
        }
        if user_input.device_id:
            body["device_id"] = user_input.device_id

        headers = {"Accept": "text/event-stream"}
        if token := self.connection_data.get(CONF_TOKEN):
            headers["Authorization"] = f"Bearer {token}"

        url = self.connection_data[CONF_BASE_URL].rstrip("/") + CONVERSE_PATH

        started = False
        async with self.session.post(
            url,
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as response:
            if response.status != 200:
                detail = (await response.text())[:500]
                _LOGGER.error("Converse backend returned HTTP %s: %s", response.status, detail)
                raise BackendStreamError(f"Converse backend returned HTTP {response.status}")

            async for sse in async_iter_sse(response.content.iter_any()):
                if sse.event == "text_delta":
                    payload = _parse_data(sse.data)
                    if payload is None:
                        continue
                    if delta := payload.get("delta"):
                        if not started:
                            yield {"role": "assistant"}
                            started = True
                        yield {"content": delta}
                elif sse.event == "error":
                    payload = _parse_data(sse.data) or {}
                    _LOGGER.error(
                        "Converse backend error event: %s — %s",
                        payload.get("code"),
                        payload.get("message"),
                    )
                    raise BackendStreamError(f"Converse backend error event: {payload.get('code')}")
                elif sse.event == "done":
                    payload = _parse_data(sse.data) or {}
                    if payload.get("continue_conversation"):
                        ctx.continue_conversation = True
                    # Deltas are authoritative; done.text is only used to voice a
                    # reply the backend produced without streaming any text_delta.
                    if not started:
                        yield {"role": "assistant"}
                        yield {"content": payload.get("text") or ERROR_MESSAGE}
                    return


def _parse_data(data: str) -> dict[str, Any] | None:
    """Parse an SSE ``data:`` payload as a JSON object, tolerating malformed lines."""
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        _LOGGER.warning("Failed to parse converse SSE data payload: %s", data)
        return None
    if isinstance(parsed, dict):
        return cast("dict[str, Any]", parsed)
    return None
