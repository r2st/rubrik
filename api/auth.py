"""API key authentication.

Pattern: a single shared secret managed at runtime via the admin panel
(`auth.api_key` runtime setting). When set, every request to `/api/v1/*`
requires an `X-API-Key` header that matches. When unset (empty), auth is
disabled — dev convenience.

Note: the *value* of the API key is no longer in env vars or bootstrap.toml.
It lives in the runtime settings store and can be rotated through the admin
panel without a restart.

For multi-tenant production, swap this for JWT — drop in a new dependency
returning the authenticated principal; downstream code doesn't need to change.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from src.settings import get_runtime_view

# auto_error=False so we control the error message + envelope ourselves
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    request: Request,
    provided: Optional[str] = Depends(_api_key_header),
) -> None:
    """FastAPI dependency that gates `/api/v1/*` endpoints behind an API key.

    No-op when no key is configured in the runtime settings store (dev mode).
    """
    runtime = get_runtime_view()
    if not runtime.auth_required:
        return
    if not provided or provided != runtime.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    request.state.authenticated = True
