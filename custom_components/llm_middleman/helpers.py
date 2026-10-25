"""Helpers shared by setup and the conversation and AI Task platforms."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import aiohttp
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .backends.base import BackendAdapter, BackendAuthError
from .const import DOMAIN, ISSUE_BACKEND_UNREACHABLE, MAX_TOOL_ITERATIONS, UNREACHABLE_ISSUE_THRESHOLD

_LOGGER = logging.getLogger(__name__)


@callback
def async_start_reauth_on_auth_error(hass: HomeAssistant, entry: ConfigEntry, err: BaseException) -> None:
    """Start the entry's reauth flow when a round-trip failed on rejected credentials (401/403)."""
    if isinstance(err, BackendAuthError):
        # HA dedupes: no second flow starts while a reauth/reconfigure one is active.
        entry.async_start_reauth(hass)


def _unreachable_issue_id(entry_id: str) -> str:
    return f"{ISSUE_BACKEND_UNREACHABLE}_{entry_id}"


@callback
def async_create_unreachable_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Raise the repair issue telling the user this connection's backend can't be reached."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _unreachable_issue_id(entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_BACKEND_UNREACHABLE,
        translation_placeholders={"title": entry.title},
        learn_more_url="https://github.com/allada-homelab/LLM-Middleman",
    )


@callback
def async_delete_unreachable_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Clear the unreachable repair issue (a no-op when none is raised)."""
    ir.async_delete_issue(hass, DOMAIN, _unreachable_issue_id(entry_id))


def is_unreachable(err: BaseException) -> bool:
    """True when ``err`` (or what it wraps) is a transport failure: refused, dropped, timed out."""
    current: BaseException | None = err
    while current is not None:
        # ServerTimeoutError (connect/read timeouts) is a ClientConnectionError too.
        if isinstance(current, aiohttp.ClientConnectionError):
            return True
        current = current.__cause__
    return False


@callback
def async_record_backend_failure(
    hass: HomeAssistant, entry: ConfigEntry, adapter: BackendAdapter, err: BaseException
) -> None:
    """Record a failed round-trip: start reauth on a rejected credential, count it if unreachable.

    The count lives on the shared adapter, so every entity of a connection agrees on one
    reachability state (quality-scale log-when-unavailable).
    """
    async_start_reauth_on_auth_error(hass, entry, err)
    if not is_unreachable(err):
        async_record_backend_reached(hass, entry, adapter)
        return
    adapter.unreachable_turns += 1
    # Log on the first unreachable round-trip, as DataUpdateCoordinator does on its first failed
    # update; only the user-facing repair issue waits out transient blips.
    if adapter.unreachable_turns == 1:
        _LOGGER.info("Backend of connection %s is unreachable: %s", entry.title, err)
    if adapter.unreachable_turns >= UNREACHABLE_ISSUE_THRESHOLD:
        async_create_unreachable_issue(hass, entry)


@callback
def async_record_backend_reached(hass: HomeAssistant, entry: ConfigEntry, adapter: BackendAdapter) -> None:
    """Record that the backend answered (even with an error): log a recovery, clear the issue.

    An open repair issue also counts as down, because a reload builds a fresh adapter whose
    count starts at 0 while the issue outlives it.
    """
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _unreachable_issue_id(entry.entry_id))
    if adapter.unreachable_turns or issue is not None:
        _LOGGER.info("Backend of connection %s is reachable again", entry.title)
    adapter.unreachable_turns = 0
    async_delete_unreachable_issue(hass, entry.entry_id)


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
