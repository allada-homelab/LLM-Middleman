"""Streaming E2E: every preset through HA's real ``async_converse`` against a live socket.

A real aiohttp server (``live_backend``) streams frames with real delays, and turns run
the way ``assist_pipeline`` drives them (chat log + delta listener). These gate the
streaming *behaviour* end to end: replies stream incrementally rather than buffering,
an actively-streaming reply is never cut off by the per-agent timeout, a cancelled turn
releases its backend connection, turns run concurrently, and arbitrary TCP chunking
(1-byte reads, LF/CRLF/CR, cuts inside multibyte UTF-8) never corrupts the reply.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.llm_middleman.const import CONF_STATELESS_RUNS, CONF_TIMEOUT, ERROR_MESSAGE
from tests.conftest import FakeStreamResponse, chunk_bytes, fake_aiohttp_session
from tests.live_backend import (
    PRESETS,
    SSE_PRESETS,
    DeltaRecorder,
    ScriptedBackend,
    content_type,
    preset_wire,
    run_pipeline_turn,
    setup_agent,
    speech,
)

PIECES = ["Hello ", "streaming ", "world."]


async def test_first_delta_arrives_long_before_backend_finishes(
    hass: HomeAssistant, live_backend: ScriptedBackend, preset: str
) -> None:
    """Each frame reaches the chat log as it is written — the reply is not buffered."""
    live_backend.serve(preset, PIECES, delays=[0.0, 0.2, 0.2])
    _, entity_id = await setup_agent(hass, preset, live_backend.base_url)
    recorder = DeltaRecorder()

    await run_pipeline_turn(hass, entity_id, recorder)

    assert recorder.contents == PIECES
    # The last frame is written 400 ms after the first, so a buffering adapter would deliver
    # the first delta only after it; 150 ms of slack keeps CI jitter from flaking.
    assert recorder.content_times[0] < live_backend.writes[-1] - 0.15


async def test_live_stream_outlasting_agent_timeout_delivers_every_frame(
    hass: HomeAssistant, live_backend: ScriptedBackend, preset: str
) -> None:
    """A reply still producing tokens past ``CONF_TIMEOUT`` is not cut off (no ``total`` deadline)."""
    pieces = [f"tok{i} " for i in range(4)]
    live_backend.serve(preset, pieces, delays=[0.3] * 4)  # ~1.2 s of live streaming
    _, entity_id = await setup_agent(hass, preset, live_backend.base_url, **{CONF_TIMEOUT: 1})

    result = await run_pipeline_turn(hass, entity_id)

    assert speech(result) == "".join(pieces)


async def _cancel_mid_stream(hass: HomeAssistant, backend: ScriptedBackend, preset: str) -> DeltaRecorder:
    """Start a turn, cancel it right after the first delta, and return its recorded deltas."""
    backend.serve(preset, PIECES, delays=[0.05, 5.0, 5.0])
    _, entity_id = await setup_agent(hass, preset, backend.base_url)
    recorder = DeltaRecorder()
    turn = asyncio.create_task(run_pipeline_turn(hass, entity_id, recorder))
    await asyncio.wait_for(recorder.first_content.wait(), timeout=3)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    return recorder


async def test_cancelled_turn_releases_backend_connection(
    hass: HomeAssistant, live_backend: ScriptedBackend, preset: str
) -> None:
    """Cancelling a turn mid-reply closes the backend connection instead of leaking it."""
    await _cancel_mid_stream(hass, live_backend, preset)

    # The next frame is 5 s away, so a disconnect within 2 s can only be the client hanging up.
    await asyncio.wait_for(live_backend.disconnected.wait(), timeout=2)
    assert live_backend.disconnected.is_set()


async def test_cancelled_turn_appends_no_fallback(
    hass: HomeAssistant, live_backend: ScriptedBackend, preset: str
) -> None:
    """Cancellation is not a backend failure: the error fallback must not be spoken."""
    recorder = await _cancel_mid_stream(hass, live_backend, preset)

    assert recorder.contents == PIECES[:1]
    assert ERROR_MESSAGE not in recorder.contents


async def test_concurrent_turns_stream_in_parallel(hass: HomeAssistant, live_backend: ScriptedBackend) -> None:
    """Five simultaneous turns overlap on the loop rather than serializing."""
    live_backend.serve("openai_compat", PIECES, delays=[0.2, 0.2, 0.2])
    _, entity_id = await setup_agent(hass, "openai_compat", live_backend.base_url)
    started = time.monotonic()

    results = await asyncio.gather(*(run_pipeline_turn(hass, entity_id) for _ in range(5)))

    assert [speech(result) for result in results] == ["".join(PIECES)] * 5
    # One turn takes ~0.6 s; serialized, five would take >= 3 s.
    assert time.monotonic() - started < 2.0


# --- chunk-boundary robustness ----------------------------------------------------

# 2-, 3- and 4-byte UTF-8 characters plus an embedded newline inside the content.
TRICKY_PIECES = ["héllo ", "wörld 世界 ", "🎉 done\n", " end"]


def _random_cuts(seed: int) -> Callable[[bytes], list[bytes]]:
    def split(blob: bytes) -> list[bytes]:
        cuts = random.Random(seed).sample(range(1, len(blob)), 25)  # noqa: S311 — deterministic test data
        return chunk_bytes(blob, sorted(cuts))

    return split


def _cut_inside_every_multibyte_char(blob: bytes) -> list[bytes]:
    # Split right after each UTF-8 lead byte, so every multibyte character spans two chunks.
    return chunk_bytes(blob, [index + 1 for index, byte in enumerate(blob) if byte >= 0xC0])


SPLITTERS: dict[str, Callable[[bytes], list[bytes]]] = {
    "whole": lambda blob: [blob],
    "1byte": lambda blob: chunk_bytes(blob, 1),
    "mid-utf8": _cut_inside_every_multibyte_char,
    "random": _random_cuts(1),
}
NEWLINES = {"LF": b"\n", "CRLF": b"\r\n", "CR": b"\r"}
CHUNK_CASES = [
    pytest.param(preset, newline, splitter, id=f"{preset}-{newline}-{splitter}")
    for preset in PRESETS
    # A bare CR line ending is legal SSE but not NDJSON.
    for newline in (NEWLINES if preset in SSE_PRESETS else ("LF", "CRLF"))
    for splitter in SPLITTERS
]
# Stateless LangGraph runs skip thread creation, so the single fake response is the reply.
_CHUNK_OPTIONS: dict[str, dict[str, Any]] = {"langgraph": {CONF_STATELESS_RUNS: True}}


@pytest.mark.parametrize(("preset", "newline", "splitter"), CHUNK_CASES)
async def test_reply_survives_arbitrary_chunk_boundaries(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, preset: str, newline: str, splitter: str
) -> None:
    """However the transport chunks the bytes, the spoken reply is exactly the streamed text."""
    blob = preset_wire(preset, TRICKY_PIECES, NEWLINES[newline]).blob()
    response = FakeStreamResponse(SPLITTERS[splitter](blob), headers={"Content-Type": content_type(preset)})
    entry, entity_id = await setup_agent(hass, preset, "http://backend.invalid", **_CHUNK_OPTIONS.get(preset, {}))
    monkeypatch.setattr(entry.runtime_data, "session", fake_aiohttp_session(response=response))

    result = await run_pipeline_turn(hass, entity_id)

    assert speech(result) == "".join(TRICKY_PIECES)
