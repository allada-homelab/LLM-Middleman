"""Backend adapter package: the ``BACKEND_TO_CLS`` factory.

``BACKEND_TO_CLS`` maps each backend-type key to its adapter class. It is **empty**
until the first concrete adapter lands (LLMM-008). Each adapter ticket registers its
class with a one-line change: import the module and add its class to the tuple, e.g.

    from .openai_compat import OpenAICompatAdapter

    BACKEND_TO_CLS = {
        cls.backend_type: cls
        for cls in (OpenAICompatAdapter,)
    }

Import direction (no cycle): ``base`` ← concrete adapters ← this module.
"""

from __future__ import annotations

from .base import BackendAdapter
from .converse import ConverseAdapter

__all__ = ["BACKEND_TO_CLS", "BackendAdapter", "get_backend_cls"]

# Registered adapter classes, keyed by their ``backend_type`` classvar. Each adapter
# ticket appends its class to the tuple (see module docstring for the convention).
BACKEND_TO_CLS: dict[str, type[BackendAdapter]] = {cls.backend_type: cls for cls in (ConverseAdapter,)}


def get_backend_cls(backend_type: str) -> type[BackendAdapter]:
    """Return the adapter class for ``backend_type``; raise for an unknown type."""
    try:
        return BACKEND_TO_CLS[backend_type]
    except KeyError:
        raise ValueError(f"Unknown backend type: {backend_type!r}") from None
