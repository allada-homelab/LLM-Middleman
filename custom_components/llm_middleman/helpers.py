"""Entity-side helpers shared by the conversation and AI Task platforms."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .backends.base import BackendAuthError
from .const import MAX_TOOL_ITERATIONS


@callback
def async_start_reauth_on_auth_error(hass: HomeAssistant, entry: ConfigEntry, err: BaseException) -> None:
    """Start the entry's reauth flow when a round-trip failed on rejected credentials (401/403)."""
    if isinstance(err, BackendAuthError):
        # HA dedupes: no second flow starts while a reauth/reconfigure one is active.
        entry.async_start_reauth(hass)


async def async_run_tool_loop(chat_log: conversation.ChatLog, run_round: Callable[[], Awaitable[object]]) -> bool:
    """Drive the bounded HA tool loop (core openai/ollama shape); False if the cap was hit.

    ``run_round`` performs one adapter round-trip into ``chat_log``; HA runs any tool calls
    it emitted and appends their results. The loop re-enters while the last content is an
    unresponded tool result, so a text-only reply stops after exactly one round. The
    ``MAX_TOOL_ITERATIONS`` cap is the HA-side backstop against a runaway tool-calling
    backend; the caller decides how to answer when it is hit.
    """
    for _ in range(MAX_TOOL_ITERATIONS):
        await run_round()
        if not chat_log.unresponded_tool_results:
            return True
    return False
