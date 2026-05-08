"""Tests for TTL jitter + single-flight cache helpers (ADR 0014)."""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from api.caching import SingleFlight, ttl_with_jitter


# ---------------------------------------------------------------------------
# ttl_with_jitter
# ---------------------------------------------------------------------------
def test_ttl_jitter_stays_within_band():
    base = 100
    pct = 0.10
    samples = [ttl_with_jitter(base, jitter_pct=pct) for _ in range(500)]
    lo, hi = base * (1 - pct), base * (1 + pct)
    assert all(lo - 1 <= s <= hi + 1 for s in samples), \
        f"min={min(samples)}, max={max(samples)}"


def test_ttl_jitter_actually_decorrelates():
    """500 calls should produce many distinct values, not all the same.

    With base=100 and ±10%, integer outputs span [90, 110] — 21 possible
    values. Require ≥10 to confirm randomization isn't trivially broken.
    """
    distinct = {ttl_with_jitter(100) for _ in range(500)}
    assert len(distinct) >= 10, f"ttl jitter is not actually randomizing: {distinct}"


def test_ttl_jitter_disabled_returns_base():
    assert ttl_with_jitter(100, jitter_pct=0) == 100
    assert ttl_with_jitter(100, jitter_pct=-1) == 100


def test_ttl_jitter_minimum_is_one():
    """Even a tiny base + large jitter never returns ≤ 0."""
    for _ in range(100):
        assert ttl_with_jitter(1, jitter_pct=2.0) >= 1


# ---------------------------------------------------------------------------
# SingleFlight
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_single_flight_coalesces_concurrent_misses():
    """N concurrent .do(key) calls run the loader exactly once."""
    sf = SingleFlight()
    counter = Counter()

    async def loader():
        counter["calls"] += 1
        await asyncio.sleep(0.05)
        return "value"

    # Fan out 20 concurrent callers for the same key.
    results = await asyncio.gather(*[sf.do("k", loader) for _ in range(20)])
    assert results == ["value"] * 20
    assert counter["calls"] == 1, "loader should only run once"


@pytest.mark.asyncio
async def test_single_flight_separates_distinct_keys():
    """Different keys do NOT share — each runs its own loader."""
    sf = SingleFlight()
    calls = Counter()

    async def loader(k):
        calls[k] += 1
        await asyncio.sleep(0.01)
        return f"v-{k}"

    a, b = await asyncio.gather(
        sf.do("a", lambda: loader("a")),
        sf.do("b", lambda: loader("b")),
    )
    assert a == "v-a"
    assert b == "v-b"
    assert calls == Counter({"a": 1, "b": 1})


@pytest.mark.asyncio
async def test_single_flight_propagates_loader_errors_to_all_waiters():
    """If the leader's loader raises, every waiter sees the same exception."""
    sf = SingleFlight()

    async def boom():
        await asyncio.sleep(0.01)
        raise ValueError("upstream failed")

    results = await asyncio.gather(
        *[sf.do("k", boom) for _ in range(5)],
        return_exceptions=True,
    )
    assert all(isinstance(r, ValueError) for r in results)


@pytest.mark.asyncio
async def test_single_flight_releases_key_after_completion():
    """A second wave for the same key after the first finished runs again."""
    sf = SingleFlight()
    calls = Counter()

    async def loader():
        # Yield once so concurrent waiters in the first wave actually
        # observe the shared inflight future. Without an await,
        # single-flight has nothing to coalesce on a single-threaded loop.
        await asyncio.sleep(0.01)
        calls["calls"] += 1
        return "v"

    # First wave — coalesced into one.
    await asyncio.gather(*[sf.do("k", loader) for _ in range(3)])
    assert calls["calls"] == 1
    # Second wave after a small gap — must run the loader again.
    await sf.do("k", loader)
    assert calls["calls"] == 2
