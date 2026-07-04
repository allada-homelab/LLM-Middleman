"""Shared provider-message history trimming (ollama-style).

Stateless-replay adapters (openai-compat LLMM-008, ollama LLMM-010) rebuild the
provider ``messages[]`` from ``chat_log.content`` every turn. To bound the context
window they trim old rounds with the same rule Home Assistant's core ollama
integration uses (``homeassistant/components/ollama/entity.py:_trim_history``):
keep the system message plus the last ``2 * max_history + 1`` messages (one full
round is a user + assistant pair, plus the in-progress user turn).

This is the single trim code path both adapters share — do not fork a second copy.
The helper is provider-shape-agnostic: it only reads each message's ``role`` field,
which both the openai-compat and ollama message shapes carry.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def trim_history(messages: Sequence[dict[str, Any]], max_history: int) -> list[dict[str, Any]]:
    """Return ``messages`` trimmed to the system message + last ``2*max_history+1``.

    ``max_history < 1`` keeps everything (no trim). Trimming only kicks in once the
    number of *previous* user rounds reaches ``max_history``; below that the full
    list is returned unchanged. The first message is preserved only when it is the
    system prompt, mirroring the core-ollama convention that index 0 is the system
    message.
    """
    result = list(messages)
    if max_history < 1:
        return result

    # Exclude the in-progress (current) user turn from the round count.
    num_previous_rounds = sum(1 for message in result if message.get("role") == "user") - 1
    if num_previous_rounds < max_history:
        return result

    num_keep = 2 * max_history + 1
    drop_index = len(result) - num_keep
    head = [result[0]] if result and result[0].get("role") == "system" else []
    return [*head, *result[drop_index:]]
