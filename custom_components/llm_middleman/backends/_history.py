"""Shared history-trimming for stateless-replay adapters.

Stateless backends (openai-compat, ollama) rebuild the provider ``messages[]``
from ``chat_log.content`` every turn, so an unbounded HA session would keep
growing the request. This mirrors core ollama's ``_trim_history`` (keep the
system prompt + the last ``2 * max_history + 1`` messages) so LLMM-010 reuses the
identical logic instead of forking a second copy.

It operates on already-converted provider message dicts (each a ``{"role": …}``
mapping), so it is provider-shape-agnostic: the openai/ollama message layouts
differ, but the trimming arithmetic is the same.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def trim_history(messages: Sequence[dict[str, Any]], max_history: int) -> list[dict[str, Any]]:
    """Return ``messages`` trimmed to the system prompt + last ``2*max_history+1``.

    ``max_history < 1`` keeps everything (no trimming). Otherwise, when the list
    is longer than ``num_keep = 2*max_history+1``, the leading message is kept
    only when it is the system prompt (HA seeds ``content[0]`` with a
    ``SystemContent``), followed by the last ``num_keep`` messages — matching core
    ollama's ``_trim_history`` slice.
    """
    if max_history < 1:
        return list(messages)
    num_keep = 2 * max_history + 1
    if len(messages) <= num_keep:
        return list(messages)
    tail = list(messages[len(messages) - num_keep :])
    if messages[0].get("role") == "system":
        return [messages[0], *tail]
    return tail
