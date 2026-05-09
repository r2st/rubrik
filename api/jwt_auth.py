"""JWT verification — opt-in alongside the existing API-key dependency.

Closes the migration path documented in ADR 0006: the application code's
shape doesn't have to change when JWT lands; routes already depend on a
``require_*`` callable. This module provides ``require_jwt`` that ADR
0006's "When to revisit" triggers can swap in for ``require_api_key``.

Operator surface (via ``/admin``):
  - ``auth.jwt_enabled``        bool   default ``false``
  - ``auth.jwt_algorithm``      str    default ``HS256``
  - ``auth.jwt_secret``         secret (HS256 only)
  - ``auth.jwt_jwks_url``       str    (RS256/ES256 — fetched on first use)
  - ``auth.jwt_audience``       str    optional ``aud`` claim check
  - ``auth.jwt_issuer``         str    optional ``iss`` claim check

Behaviour
---------
- If ``auth.jwt_enabled`` is False (default), the dependency is a no-op
  — the existing API-key path remains the only auth check.
- Otherwise: extract Bearer token from ``Authorization`` header,
  validate signature + ``exp`` + ``aud``/``iss`` (when configured).
  Return the decoded claims dict; the route can use ``request.state.jwt``
  to access them downstream.

The JWT path co-exists with API-key auth — operators flip ``jwt_enabled``
on once they've configured the upstream issuer; they can run with both
checks simultaneously during the migration window.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.logging_config import get_logger

log = get_logger(__name__)

_bearer = HTTPBearer(auto_error=False)


# Cache the JWKS keys — re-fetching on every request is expensive and racy.
_jwks_cache: dict[str, tuple[dict, float]] = {}
_JWKS_TTL_SECONDS = 600  # 10 minutes — short enough for key rotations


def _runtime() -> dict[str, Any]:
    """Snapshot the JWT-relevant runtime settings into a plain dict."""
    try:
        from src.runtime_settings import get_runtime
        rt = get_runtime()
        return {
            "enabled": bool(rt.get("auth.jwt_enabled", False)),
            "algorithm": str(rt.get("auth.jwt_algorithm", "HS256")),
            "secret": str(rt.get("auth.jwt_secret", "") or ""),
            "jwks_url": str(rt.get("auth.jwt_jwks_url", "") or ""),
            "audience": str(rt.get("auth.jwt_audience", "") or "") or None,
            "issuer": str(rt.get("auth.jwt_issuer", "") or "") or None,
        }
    except Exception:  # noqa: BLE001
        return {"enabled": False}


def _fetch_jwks(url: str) -> dict:
    """Fetch + cache the JWKS document. Cached for ``_JWKS_TTL_SECONDS``."""
    now = time.time()
    cached = _jwks_cache.get(url)
    if cached and now - cached[1] < _JWKS_TTL_SECONDS:
        return cached[0]
    import urllib.request
    with urllib.request.urlopen(url, timeout=2.0) as resp:
        import json
        body = json.loads(resp.read().decode("utf-8"))
    _jwks_cache[url] = (body, now)
    return body


async def require_jwt(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = None,  # noqa: ARG001
) -> Optional[dict]:
    """FastAPI dependency that validates a Bearer JWT when enabled.

    Returns the decoded claims dict on success, or ``None`` when JWT auth
    is disabled (the legacy API-key check still runs alongside on routes
    that depend on both). Raises 401 on signature/expiry/audience/issuer
    failures.
    """
    # FastAPI doesn't pass `credentials` through `Depends(...)` chain
    # cleanly, so we re-read it from the header here.
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    else:
        token = ""

    cfg = _runtime()
    if not cfg["enabled"]:
        return None

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
        )

    try:
        import jwt as _jwt
    except ImportError as e:  # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail=(
                "JWT auth enabled but the 'pyjwt' package isn't installed. "
                "Install pyjwt[crypto] to use this auth path."
            ),
        ) from e

    algo = cfg["algorithm"]
    options = {"verify_aud": cfg["audience"] is not None}
    decode_kwargs: dict[str, Any] = {
        "algorithms": [algo],
        "options": options,
    }
    if cfg["audience"]:
        decode_kwargs["audience"] = cfg["audience"]
    if cfg["issuer"]:
        decode_kwargs["issuer"] = cfg["issuer"]

    if algo.startswith("HS"):
        if not cfg["secret"]:
            raise HTTPException(503, "JWT secret not configured")
        try:
            claims = _jwt.decode(token, cfg["secret"], **decode_kwargs)
        except _jwt.ExpiredSignatureError as e:
            raise HTTPException(401, "Token expired") from e
        except _jwt.InvalidTokenError as e:
            log.debug("JWT validation failed: %s", e)
            raise HTTPException(401, "Invalid token") from e
    else:
        # RS/ES — fetch JWKS, find the right key by `kid`.
        if not cfg["jwks_url"]:
            raise HTTPException(503, "JWT JWKS URL not configured")
        try:
            unverified = _jwt.get_unverified_header(token)
        except _jwt.InvalidTokenError as e:
            raise HTTPException(401, "Malformed token") from e
        kid = unverified.get("kid")
        jwks = _fetch_jwks(cfg["jwks_url"])
        keys = {k.get("kid"): k for k in jwks.get("keys", [])}
        key = keys.get(kid) if kid else next(iter(keys.values()), None)
        if key is None:
            raise HTTPException(401, "No matching JWKS key")
        try:
            from jwt.algorithms import RSAAlgorithm
            public_key = RSAAlgorithm.from_jwk(key)
            claims = _jwt.decode(token, public_key, **decode_kwargs)
        except _jwt.ExpiredSignatureError as e:
            raise HTTPException(401, "Token expired") from e
        except _jwt.InvalidTokenError as e:
            log.debug("JWT validation failed: %s", e)
            raise HTTPException(401, "Invalid token") from e

    # Stash claims so route handlers can read them via request.state.jwt
    request.state.jwt = claims
    return claims


# Make HTTPBearer's silent presence visible to OpenAPI even when JWT is off.
# (`require_jwt` short-circuits when disabled, so /docs is happy either way.)
_ = _bearer
