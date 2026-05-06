"""API key authentication.

Pattern: a single shared secret in `Settings.api_key`. When set, every request
to `/api/*` requires an `X-API-Key` header that matches. When unset, auth is
disabled (dev-friendly).

For multi-tenant production, swap this for JWT — drop in a new dependency
returning the authenticated principal; downstream code doesn't need to change.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from src.settings import Settings, get_settings

# auto_error=False so we control the error message + envelope ourselves
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    request: Request,
    provided: Optional[str] = Depends(_api_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    """FastAPI dependency that gates `/api/*` endpoints behind an API key.

    No-op when `settings.api_key` is unset (dev mode).
    """
    if not settings.auth_required:
        return
    if not provided or provided != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    # Stash on request state for downstream handlers / logging
    request.state.authenticated = True
