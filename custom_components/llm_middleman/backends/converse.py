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

Terminal states (LLMM-025, matching Dify): ``done`` is the only success. ``error`` and EOF
without ``done`` raise, so a dropped stream is never passed off as a complete reply. The
optional ``tool_activity`` events are shown in the Assist chat as external tool calls when
they precede the spoken text, and recorded in the conversation trace otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

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
)

from ._compat import AssistantDelta, external_tool_call_delta, external_tool_result_delta
from ._sse import BackendStreamError, async_iter_sse
from .base import (
    BackendAdapter,
    BackendAuthError,
    BackendConnectionError,
    DeltaStream,
    TurnContext,
    bearer_headers,
    build_client_timeout,
    json_object,
    loads_json_object,
)

_LOGGER = logging.getLogger(__name__)


def _tool_activity_deltas(activity: list[dict[str, Any]]) -> list[AssistantDelta]:
    """Render buffered ``tool_activity`` payloads as external tool call + result deltas.

    The contract gives no call id, so the optional ``id`` is used when sent, else a per-turn
    ``converse-tool-N``; the result is the reported ``status`` (anything but ``ok`` is an error).
    """
    deltas: list[AssistantDelta] = []
    for index, payload in enumerate(activity, start=1):
        call_id = payload.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"converse-tool-{index}"
        name: str = payload["tool"]
        status = payload.get("status", "ok")
        deltas.append(external_tool_call_delta(call_id, name, json_object(payload.get("args")) or {}))
        deltas.append(external_tool_result_delta(call_id, name, {"status": status}, error=status != "ok"))
    return deltas


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

        Any other HTTP response from the base URL proves the transport is reachable (the
        contract defines only ``POST /v1/converse``, so ``GET /`` routinely 404s), except
        401/403: the backend rejected the bearer token → :class:`BackendAuthError` (the config
        flow's ``invalid_auth``). Connection/timeout failures map to
        :class:`BackendConnectionError` (``cannot_connect``).
        """
        session = async_get_clientsession(hass)
        try:
            async with session.get(
                data[CONF_BASE_URL],
                headers=bearer_headers(data.get(CONF_TOKEN)),
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as response:
                status = response.status
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BackendConnectionError(f"Cannot reach converse backend: {err}") from err
        if status in (401, 403):
            raise BackendAuthError(f"Converse backend rejected the token (HTTP {status})")

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

        A stream that ends without ``done`` (dropped connection, proxy cut, crashed backend)
        raises too, even after deltas: the guard appends the fallback after the partial
        reply and marks the turn failed instead of passing it off as complete. A ``done``
        with nothing to say yields nothing, so the guard's translated fallback applies.

        ``tool_activity`` is buffered and flushed as external tool deltas right before the
        first spoken text; activity after text has started (the spec example's order) or
        with no text at all is traced as ``converse_tool_activity`` instead. Placing it there
        would end the stream on a tool result (re-entering the entity's tool loop, which
        re-sends the turn) or split the reply so the spoken result loses its first part.
        """
        body: dict[str, Any] = {
            "conversation_id": ctx.memory_key,
            "text": user_input.text,
            "language": user_input.language,
        }
        if user_input.device_id:
            body["device_id"] = user_input.device_id

        headers = {"Accept": "text/event-stream", **bearer_headers(self.connection_data.get(CONF_TOKEN))}

        url = self.connection_data[CONF_BASE_URL].rstrip("/") + CONVERSE_PATH

        started = False
        pending_tools: list[dict[str, Any]] = []  # tool_activity awaiting the first spoken text
        late_tools: list[dict[str, Any]] = []  # tool_activity that cannot be placed: traced
        async with self._post_checked(
            url,
            label="Converse backend",
            json=body,
            headers=headers,
            timeout=build_client_timeout(ctx.options),
        ) as response:
            async for sse in async_iter_sse(response.content.iter_any()):
                if sse.event == "text_delta":
                    payload = loads_json_object(sse.data, warn_label="converse SSE")
                    if payload is None:
                        continue
                    if delta := payload.get("delta"):
                        if not started:
                            for tool_delta in _tool_activity_deltas(pending_tools):
                                yield tool_delta
                            pending_tools.clear()
                            yield {"role": "assistant"}
                            started = True
                        yield {"content": delta}
                elif sse.event == "tool_activity":
                    payload = loads_json_object(sse.data, warn_label="converse SSE")
                    if payload is None or not isinstance(payload.get("tool"), str) or not payload["tool"]:
                        continue
                    (late_tools if started else pending_tools).append(payload)
                elif sse.event == "error":
                    payload = loads_json_object(sse.data, warn_label="converse SSE") or {}
                    code = payload.get("code") or "no code"
                    _LOGGER.error(
                        "Converse backend error event: %s — %s",
                        code,
                        payload.get("message") or "no message",
                    )
                    raise BackendStreamError(f"Converse backend error event: {code}")
                elif sse.event == "done":
                    payload = loads_json_object(sse.data, warn_label="converse SSE") or {}
                    if payload.get("continue_conversation"):
                        ctx.continue_conversation = True
                    # Deltas are authoritative; done.text is only used to voice a
                    # reply the backend produced without streaming any text_delta.
                    text = payload.get("text")
                    if not started and isinstance(text, str) and text:
                        for tool_delta in _tool_activity_deltas(pending_tools):
                            yield tool_delta
                        pending_tools.clear()
                        yield {"role": "assistant"}
                        yield {"content": text}
                    elif not started:
                        _LOGGER.warning("Converse backend sent done with no reply text")
                    if unplaced := pending_tools + late_tools:
                        chat_log.async_trace({"converse_tool_activity": unplaced})
                    return
        # ``done`` is the contract's only success terminal; plain EOF is a dropped stream.
        raise BackendStreamError("Converse stream ended without done")
