"""Unit tests for the public test helpers in ``llm_middleman.testing``."""

from __future__ import annotations

import pytest

from llm_middleman.testing import InMemoryStore, ResponseQueue


def test_in_memory_store_assigns_incrementing_ids() -> None:
    store: InMemoryStore[str] = InMemoryStore()
    first = store.add("a")
    second = store.add("b")
    assert (first, second) == (1, 2)


def test_in_memory_store_get_returns_stored_item() -> None:
    store: InMemoryStore[str] = InMemoryStore()
    item_id = store.add("hello")
    assert store.get(item_id) == "hello"


def test_in_memory_store_get_returns_none_for_unknown_id() -> None:
    store: InMemoryStore[str] = InMemoryStore()
    assert store.get(99) is None


def test_in_memory_store_values_preserves_insertion_order() -> None:
    store: InMemoryStore[int] = InMemoryStore()
    store.add(10)
    store.add(20)
    assert store.values() == [10, 20]


def test_in_memory_store_remove_reports_whether_present() -> None:
    store: InMemoryStore[int] = InMemoryStore()
    item_id = store.add(7)
    assert store.remove(item_id) is True
    assert store.remove(item_id) is False
    assert len(store) == 0


def test_response_queue_replays_values_in_order() -> None:
    queue = ResponseQueue([1, 2, 3])
    assert [queue(), queue(), queue()] == [1, 2, 3]


def test_response_queue_raises_when_exhausted() -> None:
    queue: ResponseQueue[int] = ResponseQueue([])
    with pytest.raises(LookupError):
        queue()
