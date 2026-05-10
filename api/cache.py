"""Redis-backed read-through cache helper.

Closes the entity-level caching gap flagged in the previous round —
``customer_health:{id}``, ``meeting:{id}``, etc. don't need a custom
Redis client at every call site; this module owns the cache contract:

  - **Read-through** with a loader callback. The cache is populated on
    miss; subsequent reads bypass the loader for ``ttl_seconds`` ± jitter.
  - **TTL jitter** built in (uses ``api/caching.py::ttl_with_jitter``)
    so synchronized expiry doesn't herd.
  - **Single-flight per key** — concurrent misses for the same key share
    one underlying compute, so a Redis-flush + traffic spike doesn't
    stampede the origin.
  - **Namespaced keys** — every helper takes a ``namespace`` so
    cache invalidation can target a class of entities at once.
  - **In-process fallback** — when Redis is unset or unreachable, the
    helper degrades to a per-process LRU. Production-incorrect (no
    cross-replica sharing) but keeps dev workflows working.
  - **Negative cache** — `None` results are cached too, with a shorter
    TTL, so a parade of "give me meeting xyz that doesn't exist" doesn't
    hammer the source.

The helper is async-first because callers are FastAPI routes; the
sync variant exists for the relayer / batch paths.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional, TypeVar

from src.logging_config import get_logger

from .caching import SingleFlight, ttl_with_jitter

log = get_logger(__name__)

T = TypeVar("T")

_DEFAULT_TTL_SECONDS = 300            # 5 minutes
_NEGATIVE_TTL_SECONDS = 30
_LOCAL_LRU_CAPACITY = 4096


class _LocalLRU:
    """Tiny in-process LRU. Used only when Redis isn't available."""

    def __init__(self, capacity: int = _LOCAL_LRU_CAPACITY) -> None:
        self._cap = capacity
        self._lock = threading.Lock()
        self._d: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            entry = self._d.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.time():
                self._d.pop(key, None)
                return None
            self._d.move_to_end(key)  # mark recently used
            return value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        with self._lock:
            self._d[key] = (time.time() + ttl_seconds, value)
            self._d.move_to_end(key)
            while len(self._d) > self._cap:
                self._d.popitem(last=False)  # evict oldest

    def invalidate(self, prefix: str) -> int:
        """Drop everything whose key starts with ``prefix``. Returns count."""
        with self._lock:
            doomed = [k for k in self._d if k.startswith(prefix)]
            for k in doomed:
                self._d.pop(k, None)
            return len(doomed)


# Module-level instances so all callers share the same in-process cache
# + single-flight handle.
_local = _LocalLRU()
_singleflight = SingleFlight()


def _redis():
    """Return a Redis client when configured, else None."""
    try:
        from src.settings import get_settings
        url = get_settings().redis_url
        if not url:
            return None
        import redis
        return redis.Redis.from_url(url, socket_timeout=0.25)
    except Exception:  # noqa: BLE001
        return None


def _qualified_key(namespace: str, key: str) -> str:
    return f"{namespace}:{key}"


# ---------------------------------------------------------------------------
# Public API — synchronous get/set + async read-through
# ---------------------------------------------------------------------------
def cache_get(namespace: str, key: str) -> Optional[str]:
    """Fetch a JSON-string from cache. Returns None on miss."""
    qkey = _qualified_key(namespace, key)
    client = _redis()
    if client is not None:
        try:
            raw = client.get(qkey)
            return raw.decode("utf-8") if isinstance(raw, bytes) else raw
        except Exception:  # noqa: BLE001
            pass
    return _local.get(qkey)


def cache_set(
    namespace: str,
    key: str,
    value: str,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> None:
    """Write to cache with jittered TTL. Best-effort — failures don't raise."""
    ttl = ttl_with_jitter(ttl_seconds)
    qkey = _qualified_key(namespace, key)
    client = _redis()
    if client is not None:
        try:
            client.setex(qkey, ttl, value)
            return
        except Exception:  # noqa: BLE001
            pass
    _local.set(qkey, value, ttl)


def cache_invalidate(namespace: str, key: Optional[str] = None) -> int:
    """Drop a single key, or every key in a namespace if ``key`` is None."""
    if key is not None:
        qkey = _qualified_key(namespace, key)
        client = _redis()
        if client is not None:
            try:
                return int(bool(client.delete(qkey)))
            except Exception:  # noqa: BLE001
                pass
        return _local.invalidate(qkey)

    # Namespace-wide invalidation
    prefix = f"{namespace}:"
    client = _redis()
    if client is not None:
        try:
            count = 0
            for k in client.scan_iter(match=f"{prefix}*", count=500):
                client.delete(k)
                count += 1
            return count
        except Exception:  # noqa: BLE001
            pass
    return _local.invalidate(prefix)


async def get_or_load(
    namespace: str,
    key: str,
    loader: Callable[[], Awaitable[Optional[T]]],
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    negative_ttl_seconds: int = _NEGATIVE_TTL_SECONDS,
    serializer: Callable[[T], str] = json.dumps,
    deserializer: Callable[[str], T] = json.loads,
) -> Optional[T]:
    """Read-through with single-flight + TTL jitter + negative caching.

    ``loader`` returns the canonical value or ``None`` for "not found".
    ``None`` results are cached too (under a shorter TTL) to absorb
    "look up X that doesn't exist" probing.

    The single-flight is keyed by ``namespace:key`` so concurrent misses
    for the same key share one underlying ``loader()`` call across the
    whole process.
    """
    cached = cache_get(namespace, key)
    if cached is not None:
        # Empty string sentinel = "we know this is None"
        if cached == "":
            return None
        try:
            return deserializer(cached)
        except Exception:  # noqa: BLE001
            log.warning("Cache deserialize failed (%s:%s); falling through",
                        namespace, key)

    # Miss — collapse concurrent loaders.
    sf_key = f"{namespace}:{key}"
    return await _singleflight.do(sf_key, lambda: _do_load(
        namespace, key, loader,
        ttl_seconds, negative_ttl_seconds,
        serializer,
    ))


async def _do_load(
    namespace: str,
    key: str,
    loader: Callable[[], Awaitable[Optional[T]]],
    ttl_seconds: int,
    negative_ttl_seconds: int,
    serializer: Callable[[T], str],
) -> Optional[T]:
    value = await loader()
    if value is None:
        # Negative cache — short TTL so a slow-arriving create still lands.
        cache_set(namespace, key, "", ttl_seconds=negative_ttl_seconds)
        return None
    try:
        cache_set(namespace, key, serializer(value), ttl_seconds=ttl_seconds)
    except Exception:  # noqa: BLE001
        log.warning("Cache serialize failed (%s:%s); returning loader result",
                    namespace, key)
    return value


# ---------------------------------------------------------------------------
# Convenience hash helper for content-addressed caching
# ---------------------------------------------------------------------------
def content_hash(*parts: Any) -> str:
    """Stable SHA-256 over ``json.dumps`` of every part. 16-hex prefix."""
    h = hashlib.sha256()
    h.update(json.dumps(parts, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()[:16]
