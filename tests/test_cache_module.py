"""Tests for the Redis read-through cache helper (api/cache.py)."""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest


@pytest.fixture(autouse=True)
def _flush_local_cache():
    """Clear the in-process LRU between tests."""
    from api.cache import _local
    _local._d.clear()
    yield
    _local._d.clear()


# ---------------------------------------------------------------------------
# get / set / invalidate
# ---------------------------------------------------------------------------
def test_set_then_get_round_trip():
    from api.cache import cache_get, cache_set
    cache_set("ns1", "k1", "v1", ttl_seconds=60)
    assert cache_get("ns1", "k1") == "v1"


def test_get_returns_none_on_miss():
    from api.cache import cache_get
    assert cache_get("ns1", "absent") is None


def test_invalidate_single_key():
    from api.cache import cache_get, cache_invalidate, cache_set
    cache_set("ns1", "k1", "v1")
    assert cache_invalidate("ns1", "k1") == 1
    assert cache_get("ns1", "k1") is None


def test_invalidate_namespace_wide():
    from api.cache import cache_get, cache_invalidate, cache_set
    cache_set("ns1", "k1", "v1")
    cache_set("ns1", "k2", "v2")
    cache_set("ns2", "k1", "other")
    cleared = cache_invalidate("ns1")
    assert cleared == 2
    assert cache_get("ns1", "k1") is None
    assert cache_get("ns1", "k2") is None
    # Untouched namespace survives.
    assert cache_get("ns2", "k1") == "other"


def test_namespacing_isolates_keys():
    """Same key in different namespaces = different entries."""
    from api.cache import cache_get, cache_set
    cache_set("ns1", "k", "from-ns1")
    cache_set("ns2", "k", "from-ns2")
    assert cache_get("ns1", "k") == "from-ns1"
    assert cache_get("ns2", "k") == "from-ns2"


# ---------------------------------------------------------------------------
# get_or_load — read-through with single-flight + negative caching
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_or_load_caches_after_first_miss():
    from api.cache import get_or_load
    calls = Counter()

    async def loader():
        calls["n"] += 1
        return {"hello": "world"}

    a = await get_or_load("test", "k1", loader)
    b = await get_or_load("test", "k1", loader)
    assert a == {"hello": "world"}
    assert b == {"hello": "world"}
    assert calls["n"] == 1   # second read hit cache, didn't call loader


@pytest.mark.asyncio
async def test_get_or_load_negative_caches_none():
    """None is cached too, so probes for missing entries don't re-load."""
    from api.cache import get_or_load
    calls = Counter()

    async def loader():
        calls["n"] += 1

    a = await get_or_load("test", "missing", loader)
    b = await get_or_load("test", "missing", loader)
    assert a is None and b is None
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_get_or_load_coalesces_concurrent_misses():
    """Single-flight = only one loader call across N concurrent waiters."""
    from api.cache import get_or_load
    calls = Counter()

    async def loader():
        calls["n"] += 1
        await asyncio.sleep(0.02)
        return {"v": 1}

    results = await asyncio.gather(*[
        get_or_load("test", "shared", loader) for _ in range(15)
    ])
    assert all(r == {"v": 1} for r in results)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------
def test_set_expires_after_ttl():
    from api.cache import _local, cache_get, cache_set

    cache_set("ns", "k", "v", ttl_seconds=60)
    assert cache_get("ns", "k") == "v"
    # Force expiry by rewriting the entry's expiry into the past.
    qkey = "ns:k"
    expires_at, value = _local._d[qkey]
    _local._d[qkey] = (expires_at - 7200, value)
    assert cache_get("ns", "k") is None


# ---------------------------------------------------------------------------
# content_hash — stable + collision-resistant
# ---------------------------------------------------------------------------
def test_content_hash_is_stable():
    from api.cache import content_hash
    a = content_hash("anthropic", "claude", "hello world")
    b = content_hash("anthropic", "claude", "hello world")
    assert a == b
    assert len(a) == 16


def test_content_hash_collisions_unlikely():
    from api.cache import content_hash
    seen = {content_hash("model", f"prompt-{i}") for i in range(1000)}
    assert len(seen) == 1000  # no collisions in 1k entries
