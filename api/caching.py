"""HTTP caching for read endpoints — ETag + Cache-Control + 304 Not Modified.

The pipeline state changes rarely (only when `state.reload()` runs, which is
either on startup or every `PIPELINE_REFRESH_MINUTES`). We tie ETags to the
state's identity so clients can revalidate cheaply.

Strategy:
  - Compute a single weak ETag per pipeline build (a hash of the metadata)
  - Tag every cached response with that ETag + a short Cache-Control max-age
  - On If-None-Match match, return 304 with no body — the dashboard's repeat
    loads cost ~zero bandwidth

Use:
    @router.get("/summary")
    def summary(request: Request, response: Response) -> SummaryResponse:
        return cached(request, response, build_payload())
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Request, Response

from .state import get_state


def _etag_for_state() -> str:
    """Stable ETag for the current pipeline build.

    Hashes the metadata (which captures n_meetings, date range, k, silhouette).
    A new build → new ETag → cache miss. Format: weak ETag (W/"...") so we
    don't claim byte-for-byte identity.
    """
    s = get_state()
    canonical = json.dumps(s.metadata, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f'W/"{digest}"'


def cached(request: Request, response: Response, payload: Any,
           *, max_age: int = 60) -> Any:
    """Stamp ETag + Cache-Control on a response, honor If-None-Match.

    On cache hit, returns a 304 Response directly (no body, ETag preserved).
    Returning a Response from a FastAPI route bypasses response_model
    validation — exactly what we want for 304.

    Args:
        request: incoming request (read If-None-Match)
        response: outgoing response (set ETag + Cache-Control on 200 path)
        payload: the body to return on cache miss
        max_age: Cache-Control max-age in seconds (default 60s)

    Returns:
        `payload` on cache miss, or a 304 Response on cache hit.
    """
    etag = _etag_for_state()
    cache_control = f"private, max-age={max_age}, must-revalidate"

    inbound = request.headers.get("if-none-match")
    if inbound and inbound == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = cache_control
    return payload


# ---------------------------------------------------------------------------
# Cache-stampede protection (ADR 0014 §"Cache invalidation discipline")
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
import random  # noqa: E402
from typing import Awaitable, Callable, TypeVar  # noqa: E402

T = TypeVar("T")


def ttl_with_jitter(base_seconds: int, jitter_pct: float = 0.10) -> int:
    """Randomize a TTL by ±``jitter_pct`` so synchronized expiry doesn't herd.

    Without jitter, every cache entry written in the same second expires
    in the same second N seconds later — concurrent misses produce a
    thundering herd that hammers the origin. ±10% spread is enough to
    decorrelate without breaking freshness expectations.
    """
    if jitter_pct <= 0:
        return base_seconds
    delta = base_seconds * jitter_pct
    return max(1, int(base_seconds + random.uniform(-delta, delta)))  # noqa: S311


class SingleFlight:
    """Coalesce concurrent computes for the same key into one underlying call.

    When multiple coroutines miss the cache for the same key at the same
    time, only the first one runs the loader; the rest await its result.
    Closes the cache-stampede amplification gap that ETag + TTL alone leave
    open under heavy concurrent load.

    Usage::

        sf = SingleFlight()
        async def get_thing(key):
            cached = redis.get(key)
            if cached is not None:
                return cached
            value = await sf.do(key, lambda: load_from_origin(key))
            redis.setex(key, ttl_with_jitter(60), value)
            return value
    """

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def do(self, key: str, loader: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            existing = self._inflight.get(key)
            if existing is not None:
                # Another caller is already loading this key — await theirs.
                future = existing
                owner = False
            else:
                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                owner = True

        if not owner:
            return await future

        try:
            value = await loader()
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(value)
            return value
        finally:
            async with self._lock:
                self._inflight.pop(key, None)

