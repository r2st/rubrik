"""Tests for the search index (LocalSearchIndex + CachedSearchIndex)."""
from __future__ import annotations

from collections import Counter

import pytest


@pytest.fixture(autouse=True)
def _flush_cache():
    from api.cache import _local
    _local._d.clear()
    yield
    _local._d.clear()


@pytest.fixture
def populated_index():
    from api.search import LocalSearchIndex
    idx = LocalSearchIndex()
    idx.index("m-1", {
        "title": "Acme — Q3 review",
        "summary": "Customer happy with new pricing tier and recent migration.",
        "transcript_snippet": "We see strong growth on the Detect product.",
        "call_type": "external",
    })
    idx.index("m-2", {
        "title": "Northstar Pharma — escalation",
        "summary": "Outage caused frustration; customer considering competitor.",
        "transcript_snippet": "URGENT escalation about Detect performance.",
        "call_type": "external",
    })
    idx.index("m-3", {
        "title": "Internal sync — engineering",
        "summary": "Discussed Detect architecture and roadmap items.",
        "transcript_snippet": "Plan to ship the new auth model next quarter.",
        "call_type": "internal",
    })
    return idx


# ---------------------------------------------------------------------------
# LocalSearchIndex
# ---------------------------------------------------------------------------
def test_query_finds_relevant_meetings(populated_index):
    hits = populated_index.query("Detect")
    assert {h.meeting_id for h in hits} == {"m-1", "m-2", "m-3"}


def test_query_ranks_by_token_overlap(populated_index):
    """A query with multiple matching tokens scores higher."""
    hits = populated_index.query("Detect escalation outage")
    assert hits[0].meeting_id == "m-2"  # most token matches


def test_query_filters_by_call_type(populated_index):
    hits = populated_index.query("Detect", filters={"call_type": "internal"})
    assert {h.meeting_id for h in hits} == {"m-3"}


def test_query_returns_highlights(populated_index):
    hits = populated_index.query("Detect")
    for h in hits:
        if h.highlights:
            assert any("detect" in s.lower() for s in h.highlights)


def test_query_empty_string_returns_nothing(populated_index):
    assert populated_index.query("") == []


def test_query_size_caps_results(populated_index):
    hits = populated_index.query("Detect", size=2)
    assert len(hits) <= 2


def test_delete_removes_from_index(populated_index):
    populated_index.delete("m-1")
    hits = populated_index.query("Acme")
    assert all(h.meeting_id != "m-1" for h in hits)


# ---------------------------------------------------------------------------
# CachedSearchIndex — wraps backing index, caches by content-hash
# ---------------------------------------------------------------------------
def test_cached_query_hits_backing_once(populated_index):
    """Two identical queries → backing index sees one call."""
    from api.search import CachedSearchIndex

    calls = Counter()
    backing = populated_index
    original_query = backing.query

    def counting_query(*args, **kwargs):
        calls["n"] += 1
        return original_query(*args, **kwargs)
    backing.query = counting_query

    cached = CachedSearchIndex(backing)
    a = cached.query("Detect")
    b = cached.query("Detect")
    assert a == b
    assert calls["n"] == 1   # second call served from cache


def test_cached_index_invalidates_on_write(populated_index):
    from api.search import CachedSearchIndex
    cached = CachedSearchIndex(populated_index)
    cached.query("Detect")  # warm cache
    cached.index("m-4", {
        "title": "New", "summary": "Detect",
        "transcript_snippet": "", "call_type": "external",
    })
    # After write, repeating the same query must reflect the new doc.
    hits = cached.query("Detect")
    assert "m-4" in {h.meeting_id for h in hits}


def test_cached_index_invalidates_on_delete(populated_index):
    from api.search import CachedSearchIndex
    cached = CachedSearchIndex(populated_index)
    cached.query("Acme")
    cached.delete("m-1")
    hits = cached.query("Acme")
    assert all(h.meeting_id != "m-1" for h in hits)


def test_cached_index_namespacing_distinct_queries(populated_index):
    """Different queries cache separately."""
    from api.search import CachedSearchIndex
    cached = CachedSearchIndex(populated_index)
    a = cached.query("Detect")
    b = cached.query("Acme")
    assert a != b


# ---------------------------------------------------------------------------
# default_search_index — backend resolution via runtime setting
# ---------------------------------------------------------------------------
def test_default_search_index_returns_cached_local_by_default():
    from api.search import (
        CachedSearchIndex,
        LocalSearchIndex,
        default_search_index,
    )
    idx = default_search_index()
    assert isinstance(idx, CachedSearchIndex)
    assert isinstance(idx.backing, LocalSearchIndex)


def test_opensearch_falls_back_to_local_without_hosts():
    """Setting search.backend=opensearch but no hosts → local fallback."""
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    rt.set("search.backend", "opensearch", actor="test")
    rt.set("search.opensearch_hosts", "", actor="test")
    try:
        from api.search import (
            CachedSearchIndex,
            LocalSearchIndex,
            default_search_index,
        )
        idx = default_search_index()
        assert isinstance(idx, CachedSearchIndex)
        assert isinstance(idx.backing, LocalSearchIndex)
    finally:
        rt.set("search.backend", "local", actor="test-cleanup")
