"""Cross-preset adapter contract: every preset in ``BACKEND_TO_CLS`` obeys the same rules.

Each case runs the real adapter (and, where the user-visible outcome matters, the real
entity via ``async_converse``) against a live local server speaking that preset's wire
format (``tests.live_backend.preset_wire``). A new preset is enrolled automatically by
the ``preset`` fixture; it must add its wire format to ``preset_wire`` to pass.

The contract, and the regression class each clause gates:

* the adapter's first delta carries ``role`` (HA treats a role-less delta as a continuation);
* a multi-piece reply is spoken as exactly that text (wrong-field dispatch, dropped frames);
* an HTTP 401/500 on the turn request is logged and voiced as the fallback — never a
  silent empty reply (an adapter that ignores the status ends the stream silently);
* an HTTP 401/403 on the turn request asks HA to reauthenticate the entry (a 500 does
  not), so a revoked credential surfaces as a reauth prompt instead of silent fallbacks;
* a stream that ends with no content is voiced as the fallback;
* the streaming request has no ``total`` deadline (it kills a reply that is still
  streaming) but a ``sock_read`` one equal to the agent's Timeout (how long the backend
  may stay silent).
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant

from custom_components.llm_middleman.backends.base import TurnContext
from custom_components.llm_middleman.const import CONF_TIMEOUT, ERROR_MESSAGE
from tests.conftest import (
    MockChatLog,
    collect,
)
from tests.live_backend import ScriptedBackend, run_pipeline_turn, setup_agent, speech

PIECES = ["Hello ", "contract ", "world."]


class _TimeoutSpy:
    """Session stand-in that records the ``timeout`` of every POST, then forwards it."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self.timeouts: list[aiohttp.ClientTimeout] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.timeouts.append(kwargs["timeout"])
        return self._session.post(url, **kwargs)


async def test_adapter_first_delta_carries_role(
    hass: HomeAssistant, live_backend: ScriptedBackend, preset: str, mock_chat_log: MockChatLog
) -> None:
    """The adapter itself (not the entity guard) opens the assistant block with ``role``."""
    live_backend.serve(preset, PIECES)
    entry, _ = await setup_agent(hass, preset, live_backend.base_url)
    user_input = conversation.ConversationInput(
        text="hi",
        context=Context(),
        conversation_id=mock_chat_log.conversation_id,
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="conversation.agent",
    )
    ctx = TurnContext(options=next(iter(entry.subentries.values())).data, memory_key="session-1")

    deltas = await collect(entry.runtime_data.stream_turn(mock_chat_log, user_input, ctx))

    assert deltas[0] == {"role": "assistant"}


async def test_multi_piece_reply_is_spoken_verbatim(
    hass: HomeAssistant, live_backend: ScriptedBackend, preset: str
) -> None:
    """A reply streamed in several frames is spoken as exactly their concatenation."""
    live_backend.serve(preset, PIECES)
    _, entity_id = await setup_agent(hass, preset, live_backend.base_url)

    result = await run_pipeline_turn(hass, entity_id)

    assert speech(result) == "".join(PIECES)


def _integration_error_texts(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Message plus logged exception text of every ERROR record from the integration."""
    return [
        f"{record.getMessage()} | {record.exc_info[1] if record.exc_info else ''}"
        for record in caplog.records
        if record.levelno >= logging.ERROR and record.name.startswith("custom_components.llm_middleman")
    ]


@pytest.mark.parametrize("status", [401, 500])
async def test_http_error_status_is_voiced_as_fallback(
    hass: HomeAssistant, live_backend: ScriptedBackend, preset: str, status: int
) -> None:
    """A non-200 turn response is voiced as the fallback, never a silent empty reply."""
    live_backend.serve(preset, PIECES, status=status)
    _, entity_id = await setup_agent(hass, preset, live_backend.base_url)

    result = await run_pipeline_turn(hass, entity_id)

    assert speech(result) == ERROR_MESSAGE


@pytest.mark.parametrize("status", [401, 500])
async def test_http_error_status_is_logged_with_its_status_code(
    hass: HomeAssistant, live_backend: ScriptedBackend, preset: str, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    """The logged error names the HTTP status, so a fallback reached by accident can't pass."""
    live_backend.serve(preset, PIECES, status=status)
    _, entity_id = await setup_agent(hass, preset, live_backend.base_url)

    await run_pipeline_turn(hass, entity_id)

    assert any(f"HTTP {status}" in text for text in _integration_error_texts(caplog))


@pytest.mark.parametrize(("status", "reauth"), [(401, True), (403, True), (500, False)])
async def test_auth_error_status_requests_reauth(
    hass: HomeAssistant, live_backend: ScriptedBackend, preset: str, status: int, reauth: bool
) -> None:
    """A credential rejection on the turn request starts reauth; other statuses don't."""
    live_backend.serve(preset, PIECES, status=status)
    entry, entity_id = await setup_agent(hass, preset, live_backend.base_url)

    with patch.object(entry, "async_start_reauth") as start_reauth:
        result = await run_pipeline_turn(hass, entity_id)

    assert speech(result) == ERROR_MESSAGE
    assert start_reauth.called is reauth


async def test_reply_without_content_is_voiced_as_fallback(
    hass: HomeAssistant, live_backend: ScriptedBackend, preset: str
) -> None:
    """A well-formed stream that ends without any content still gets a spoken reply."""
    live_backend.serve(preset, [])
    _, entity_id = await setup_agent(hass, preset, live_backend.base_url)

    result = await run_pipeline_turn(hass, entity_id)

    assert speech(result) == ERROR_MESSAGE


async def test_streaming_request_bounds_silence_by_agent_timeout_but_no_total_deadline(
    hass: HomeAssistant, live_backend: ScriptedBackend, preset: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every request of a turn bounds silence by the agent Timeout, never the whole body."""
    live_backend.serve(preset, PIECES)
    entry, entity_id = await setup_agent(hass, preset, live_backend.base_url, **{CONF_TIMEOUT: 45})
    spy = _TimeoutSpy(entry.runtime_data.session)
    monkeypatch.setattr(entry.runtime_data, "session", spy)

    await run_pipeline_turn(hass, entity_id)

    assert spy.timeouts
    assert {(timeout.total, timeout.sock_read) for timeout in spy.timeouts} == {(None, 45)}
