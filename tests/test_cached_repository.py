"""Tests for CachedTranscriptRepository (read-through over any backing repo)."""
from __future__ import annotations

from collections import Counter
from typing import Iterator, Optional

import pytest

from src.data_loader import Meeting
from src.repository import CachedTranscriptRepository


@pytest.fixture(autouse=True)
def _flush_cache():
    from api.cache import _local
    _local._d.clear()
    yield
    _local._d.clear()


class _StubRepository:
    """Counts calls to ``get`` so we can assert the cache is doing its job."""

    def __init__(self) -> None:
        self.calls = Counter()
        self._meetings: dict[str, Meeting] = {
            "m-1": Meeting(meeting_id="m-1", info={"title": "Acme Q3"},
                           summary={"sentimentScore": 4.0}),
            "m-2": Meeting(meeting_id="m-2", info={"title": "Northstar"},
                           summary={"sentimentScore": 2.5}),
        }

    def count(self) -> int:
        return len(self._meetings)

    def get(self, meeting_id: str) -> Optional[Meeting]:
        self.calls[meeting_id] += 1
        return self._meetings.get(meeting_id)

    def stream(self, *, batch_size: int = 1000) -> Iterator[list[Meeting]]:
        yield list(self._meetings.values())

    def all(self) -> list[Meeting]:
        return list(self._meetings.values())


def test_get_caches_after_first_call():
    backing = _StubRepository()
    repo = CachedTranscriptRepository(backing)
    a = repo.get("m-1")
    b = repo.get("m-1")
    assert a is not None and a.meeting_id == "m-1"
    assert b is not None and b.title == "Acme Q3"
    assert backing.calls["m-1"] == 1   # cache hit on second call


def test_get_returns_none_for_missing():
    backing = _StubRepository()
    repo = CachedTranscriptRepository(backing)
    assert repo.get("absent") is None
    # Negative cache means a second probe doesn't re-hit the backing
    assert repo.get("absent") is None
    assert backing.calls["absent"] == 1


def test_get_distinct_keys_independent():
    backing = _StubRepository()
    repo = CachedTranscriptRepository(backing)
    repo.get("m-1")
    repo.get("m-2")
    repo.get("m-1")
    assert backing.calls == {"m-1": 1, "m-2": 1}


def test_invalidate_drops_specific_meeting():
    backing = _StubRepository()
    repo = CachedTranscriptRepository(backing)
    repo.get("m-1")
    CachedTranscriptRepository.invalidate("m-1")
    repo.get("m-1")
    assert backing.calls["m-1"] == 2


def test_stream_and_all_pass_through_unchanged():
    """Bulk paths shouldn't be cached; they should hit the backing repo each time."""
    backing = _StubRepository()
    repo = CachedTranscriptRepository(backing)
    assert len(list(repo.stream())) == 1
    assert len(repo.all()) == 2
    assert repo.count() == 2


def test_count_passes_through():
    backing = _StubRepository()
    repo = CachedTranscriptRepository(backing)
    assert repo.count() == 2
