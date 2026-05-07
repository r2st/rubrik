"""Rate-limit construction — Redis-backed when configured, in-process fallback.

Two improvements over the original ``Limiter(key_func=get_remote_address)``:

1. **Cluster-wide enforcement.** When ``[runtime].redis_url`` is set in
   ``bootstrap.toml``, slowapi's storage backend points at Redis so all
   replicas share the same counter. Without this, an attacker hitting 10
   replicas gets 10× the limit.

2. **Per-tenant fairness.** The key function is ``(tenant_id, ip)`` rather
   than just IP. Tenant identity comes from the ``X-API-Key`` header
   today (one key per tenant); when the JWT migration in ADR 0006 lands,
   it will pull the tenant claim instead.

Falls back to the in-process limiter if Redis is unreachable or unset —
the dev workflow keeps working without an extra dependency.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from src.logging_config import get_logger

log = get_logger(__name__)


def _tenant_id(request: Request) -> str:
    api_key = request.headers.get("X-API-Key", "")
    return (
        hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        if api_key else "anon"
    )


def tenant_aware_key(request: Request) -> str:
    """``(tenant_id, ip)`` composite key. Tenant defaults to ``anon`` when no key."""
    return f"{_tenant_id(request)}:{get_remote_address(request)}"


def per_tenant_rate_limit_dep(request: Request) -> None:
    """FastAPI dependency that enforces a per-tenant cap when configured.

    Reads ``rate_limit.per_tenant`` from runtime settings — a dict keyed by
    the tenant's hashed API-key prefix, with values in slowapi's standard
    ``"<count>/<window>"`` format. If the tenant has no override the
    dependency is a no-op (the global limiter's per-tenant fairness via
    ``tenant_aware_key`` is already in effect).

    Counters live in Redis when ``[runtime].redis_url`` is set (cluster-wide
    enforcement); otherwise an in-process sliding window keeps the dev path
    working with the obvious caveat that "per-process" ≠ "per-cluster."
    """
    from fastapi import HTTPException
    from fastapi import status as _status
    tenant = _tenant_id(request)
    try:
        from src.runtime_settings import get_runtime
        overrides = get_runtime().get("rate_limit.per_tenant", {}) or {}
    except Exception:  # noqa: BLE001
        overrides = {}
    cap_str = overrides.get(tenant)
    if not cap_str:
        return  # no override → fall through to the global limiter

    count, window_s = _parse_limit(cap_str)
    if _bucket_exceeds(tenant, count, window_s):
        raise HTTPException(
            status_code=_status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Per-tenant rate limit exceeded ({cap_str}).",
            headers={"Retry-After": str(window_s)},
        )


def _parse_limit(spec: str) -> tuple[int, int]:
    """``"500/minute"`` → ``(500, 60)``. Tolerant of common units."""
    count_str, _, unit = spec.partition("/")
    count = int(count_str.strip())
    unit = unit.strip().lower()
    units = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
    if unit not in units:
        raise ValueError(f"unsupported limit unit: {unit!r}")
    return count, units[unit]


# In-process fallback bucket (per-process). Maps tenant → deque of timestamps.
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402
from collections import deque as _deque  # noqa: E402

_local_buckets: dict[str, _deque] = {}
_local_lock = _threading.Lock()


def _bucket_exceeds(tenant: str, count: int, window_s: int) -> bool:
    """Sliding-window check: returns True iff this request would breach the cap."""
    redis_url = _redis_url()
    if redis_url:
        try:
            import redis as _redis  # noqa: PLC0415
            client = _redis.Redis.from_url(redis_url, socket_timeout=0.25)
            key = f"per_tenant_rl:{tenant}"
            cnt = client.incr(key)
            if cnt == 1:
                client.expire(key, window_s)
            return int(cnt) > count
        except Exception:  # noqa: BLE001 — degrade to in-process
            pass

    now = _time.monotonic()
    cutoff = now - window_s
    with _local_lock:
        bucket = _local_buckets.setdefault(tenant, _deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= count:
            return True
        bucket.append(now)
        return False


def _redis_url() -> Optional[str]:
    try:
        from src.settings import get_settings
        return get_settings().redis_url
    except Exception:  # noqa: BLE001
        return None


def per_tenant_limit(request: Request) -> str:
    """Resolve the per-request rate limit by consulting runtime overrides.

    Used by ``Limiter`` as a callable in ``default_limits``. Falls back to
    the global ``rate_limit.default`` when no tenant override is set.
    """
    tenant = _tenant_id(request)
    try:
        from src.runtime_settings import get_runtime
        rt = get_runtime()
        overrides = rt.get("rate_limit.per_tenant", {}) or {}
        per = overrides.get(tenant)
        if per:
            return str(per)
        return str(rt.get("rate_limit.default", "120/minute"))
    except Exception:  # noqa: BLE001
        return "120/minute"


# Module-level handle so route decorators can reference the limiter without
# importing api.main (which would create a circular import). main.py calls
# build_limiter() and the result lands here.
_limiter_instance: Optional[Limiter] = None


def get_limiter() -> Limiter:
    """Return the active Limiter. Lazily builds an in-process default if
    main.py hasn't initialized yet (only happens during isolated tests)."""
    global _limiter_instance
    if _limiter_instance is None:
        _limiter_instance = build_limiter(redis_url=None, default_limits=["120/minute"])
    return _limiter_instance


def build_limiter(
    redis_url: Optional[str],
    *,
    default_limits: list[str],
) -> Limiter:
    """Return a ``Limiter`` configured for cluster-wide or in-process state.

    Redis URL formats accepted: ``redis://...`` or ``rediss://...``.
    """
    storage_uri = None
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
    instance = Limiter(**kwargs)
    global _limiter_instance
    _limiter_instance = instance
    return instance
