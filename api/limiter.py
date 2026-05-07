"""Rate-limit construction — cluster-wide via Redis, in-process fallback.

This module owns three things:

1. ``tenant_aware_key`` — the slowapi key function. Composite of
   ``(tenant_id, ip)`` so different tenants get separate buckets even
   under the same global default.
2. ``build_limiter`` — constructs the slowapi ``Limiter``. Storage points
   at Redis when ``[runtime].redis_url`` is set (cluster-wide enforcement);
   degrades to in-process when Redis is absent or unreachable.
3. ``per_tenant_rate_limit_dep`` — FastAPI dependency that enforces a
   *per-tenant cap override* read from ``rate_limit.per_tenant`` runtime
   settings. Counters live in Redis when configured, in-process otherwise.

The dependency is the right hook for per-tenant overrides because slowapi's
own decorator path invokes its limit callable with no arguments, so it
can't see the request and therefore can't read the tenant.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from typing import Deque, Optional

from fastapi import HTTPException, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from src.logging_config import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tenant identity
# ---------------------------------------------------------------------------
def _tenant_id(request: Request) -> str:
    """Hashed prefix of the API key, or ``"anon"`` when no key is present."""
    api_key = request.headers.get("X-API-Key", "")
    return (
        hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        if api_key else "anon"
    )


def tenant_aware_key(request: Request) -> str:
    """``"<tenant>:<ip>"`` composite for slowapi's per-bucket keying."""
    return f"{_tenant_id(request)}:{get_remote_address(request)}"


# ---------------------------------------------------------------------------
# slowapi Limiter construction
# ---------------------------------------------------------------------------
def build_limiter(
    redis_url: Optional[str],
    *,
    default_limits: list[str],
) -> Limiter:
    """Return a ``Limiter`` whose storage points at Redis when configured."""
    storage_uri: Optional[str] = None
    if redis_url:
        try:
            import redis  # noqa: F401, PLC0415 — probe availability
            storage_uri = redis_url
            log.info("Rate limiter using Redis storage at %s", redis_url)
        except ImportError:
            log.warning(
                "Redis URL set but `redis` package not installed; "
                "falling back to in-process rate limiting."
            )
    if storage_uri is None:
        log.info("Rate limiter using in-process storage (single-replica only).")

    kwargs: dict = {
        "key_func": tenant_aware_key,
        "default_limits": default_limits,
        "headers_enabled": True,
    }
    if storage_uri:
        kwargs["storage_uri"] = storage_uri
    return Limiter(**kwargs)


# ---------------------------------------------------------------------------
# Per-tenant cap override — sliding-window enforcement
# ---------------------------------------------------------------------------
_LIMIT_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def _parse_limit(spec: str) -> tuple[int, int]:
    """``"500/minute"`` → ``(500, 60)``."""
    count_str, _, unit = spec.partition("/")
    unit = unit.strip().lower()
    if unit not in _LIMIT_UNITS:
        raise ValueError(f"unsupported limit unit: {unit!r}")
    return int(count_str.strip()), _LIMIT_UNITS[unit]


# In-process fallback bucket. Per-process (not per-cluster) — Redis is the
# correct backend for production; this exists only to keep the dev path
# functional when Redis isn't configured.
_local_buckets: dict[str, Deque[float]] = {}
_local_lock = threading.Lock()


def _redis_url() -> Optional[str]:
    try:
        from src.settings import get_settings
        return get_settings().redis_url
    except Exception:  # noqa: BLE001
        return None


def _bucket_exceeds(tenant: str, count: int, window_s: int) -> bool:
    """Sliding-window check: returns True iff this request would breach the cap."""
    url = _redis_url()
    if url:
        try:
            import redis as _redis
            client = _redis.Redis.from_url(url, socket_timeout=0.25)
            key = f"per_tenant_rl:{tenant}"
            cnt = client.incr(key)
            if cnt == 1:
                client.expire(key, window_s)
            return int(cnt) > count
        except Exception:  # noqa: BLE001 — degrade to in-process
            pass

    now = time.monotonic()
    cutoff = now - window_s
    with _local_lock:
        bucket = _local_buckets.setdefault(tenant, deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= count:
            return True
        bucket.append(now)
        return False


def per_tenant_rate_limit_dep(request: Request) -> None:
    """FastAPI dependency: enforce a per-tenant cap from runtime settings.

    Reads ``rate_limit.per_tenant`` — a dict keyed by hashed tenant ID with
    values like ``"500/minute"``. Tenants without an override fall through
    (the global limiter still applies via ``tenant_aware_key``).
    """
    tenant = _tenant_id(request)
    try:
        from src.runtime_settings import get_runtime
        overrides = get_runtime().get("rate_limit.per_tenant", {}) or {}
    except Exception:  # noqa: BLE001
        overrides = {}
    cap_str = overrides.get(tenant)
    if not cap_str:
        return  # no override → global limiter only

    count, window_s = _parse_limit(cap_str)
    if _bucket_exceeds(tenant, count, window_s):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Per-tenant rate limit exceeded ({cap_str}).",
            headers={"Retry-After": str(window_s)},
        )
