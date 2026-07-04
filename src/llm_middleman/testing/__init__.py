"""Public, importable test helpers shipped with this package.

Import these directly from your own test modules (this is *not* a conftest):

    from llm_middleman.testing import InMemoryStore, ResponseQueue

The helpers here are deliberately generic so they apply to any project scaffolded
from the template. Extend or replace them with domain-specific fakes as the
package grows.

:class:`InMemoryStore` is a typed, in-memory stand-in for a repository/table.
:class:`ResponseQueue` replays a fixed sequence of canned values, e.g. to stub a
collaborator that should return different results on successive calls.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterable

ItemT = TypeVar("ItemT")
ValueT = TypeVar("ValueT")


class InMemoryStore(Generic[ItemT]):
    """A minimal in-memory fake keyed by an auto-incrementing integer id."""

    def __init__(self) -> None:
        super().__init__()
        self._items: dict[int, ItemT] = {}
        self._next_id = 1

    def add(self, item: ItemT) -> int:
        """Store ``item`` under a fresh id and return that id."""
        item_id = self._next_id
        self._items[item_id] = item
        self._next_id += 1
        return item_id

    def get(self, item_id: int) -> ItemT | None:
        """Return the stored item, or ``None`` if ``item_id`` is unknown."""
        return self._items.get(item_id)

    def values(self) -> list[ItemT]:
        """Return all stored items in insertion order."""
        return list(self._items.values())

    def remove(self, item_id: int) -> bool:
        """Drop ``item_id``; return whether it was present."""
        return self._items.pop(item_id, None) is not None

    def __len__(self) -> int:
        return len(self._items)


class ResponseQueue(Generic[ValueT]):
    """Replay a fixed sequence of canned values, one per call."""

    def __init__(self, values: Iterable[ValueT]) -> None:
        super().__init__()
        self._values: deque[ValueT] = deque(values)

    def __call__(self) -> ValueT:
        if not self._values:
            msg = "response queue exhausted"
            raise LookupError(msg)
        return self._values.popleft()

    def __len__(self) -> int:
        return len(self._values)


__all__ = ["InMemoryStore", "ResponseQueue"]
