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


def tenant_aware_key(request: Request) -> str:
    """``(tenant_id, ip)`` composite key. Tenant defaults to ``anon`` when no key."""
    api_key = request.headers.get("X-API-Key", "")
    # Hash the key — never put the raw secret into Redis or logs.
    tenant = (
        hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        if api_key else "anon"
    )
    return f"{tenant}:{get_remote_address(request)}"


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
    return Limiter(**kwargs)
