"""Request-scoped tenant identity.

The DatabaseRepository.get(meeting_id, tenant_id=...) call site cares
about the *current request's tenant*. Plumbing that through every
function signature is noisy and bug-prone; a ContextVar carries it
across function boundaries with the same correctness guarantee as a
parameter (asyncio task isolation + threadpool context copy).

Resolution order (highest priority first):
  1. JWT claim ``tid`` or ``tenant`` (set by ``api/jwt_auth.py``).
  2. Hashed prefix of the ``X-API-Key`` header — same hash slowapi
     uses for tenant_aware_key, so rate-limit buckets and repository
     filters agree on the identity.
  3. ``None`` — single-tenant or anonymous; the repository skips
     the WHERE tenant_id filter entirely.

Single-tenant deployments never set anything and pay zero cost.
"""
from __future__ import annotations

import hashlib
from contextvars import ContextVar
from typing import Optional

_TENANT_CV: ContextVar[Optional[str]] = ContextVar("tenant_id", default=None)


def set_current_tenant(tenant_id: Optional[str]) -> None:
    """Stash the tenant id for the current asyncio task / threadpool task."""
    _TENANT_CV.set(tenant_id)


def current_tenant() -> Optional[str]:
    """Read the current tenant id. ``None`` = single-tenant / not set."""
    return _TENANT_CV.get()


def derive_tenant_id(
    *,
    jwt_claims: Optional[dict] = None,
    api_key: Optional[str] = None,
) -> Optional[str]:
    """Resolve tenant id from the highest-priority source available.

    JWT claims win over API key — a token-authenticated request has
    stronger identity than a key-based one. When neither is present we
    return ``None`` so the repository skips the filter.
    """
    if jwt_claims:
        for k in ("tid", "tenant", "tenant_id"):
            if jwt_claims.get(k):
                return str(jwt_claims[k])
    if api_key:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    return None
