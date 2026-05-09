"""Tests for the optional JWT auth dependency."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_jwt_settings():
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    rt.set("auth.jwt_enabled", False, actor="test")
    rt.set("auth.jwt_secret", "", actor="test")
    rt.set("auth.jwt_audience", "", actor="test")
    rt.set("auth.jwt_issuer", "", actor="test")
    yield
    rt.set("auth.jwt_enabled", False, actor="test-cleanup")
    rt.set("auth.jwt_secret", "", actor="test-cleanup")


def test_jwt_disabled_returns_none():
    """When jwt_enabled=false, the dependency is a no-op."""
    import asyncio

    from fastapi import Request

    from api.jwt_auth import require_jwt
    scope = {
        "type": "http", "method": "GET", "path": "/x",
        "headers": [], "query_string": b"",
    }
    req = Request(scope)
    result = asyncio.get_event_loop().run_until_complete(require_jwt(req))
    assert result is None


def test_jwt_enabled_missing_token_raises_401():
    import asyncio

    from fastapi import HTTPException, Request

    from api.jwt_auth import require_jwt
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    rt.set("auth.jwt_enabled", True, actor="test")
    rt.set("auth.jwt_secret", "test-secret-do-not-use-in-prod", actor="test")

    scope = {
        "type": "http", "method": "GET", "path": "/x",
        "headers": [], "query_string": b"",
    }
    req = Request(scope)
    with pytest.raises(HTTPException) as exc:
        asyncio.get_event_loop().run_until_complete(require_jwt(req))
    assert exc.value.status_code == 401


def test_jwt_valid_hs256_token_decodes():
    """Round-trip: encode with the secret, validate, get claims back."""
    pytest.importorskip("jwt")
    import asyncio

    import jwt as _jwt
    from fastapi import Request

    from api.jwt_auth import require_jwt
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    rt.set("auth.jwt_enabled", True, actor="test")
    rt.set("auth.jwt_secret", "test-secret-do-not-use-in-prod", actor="test")

    token = _jwt.encode(
        {"sub": "user-123", "exp": 9999999999},
        "test-secret-do-not-use-in-prod",
        algorithm="HS256",
    )
    scope = {
        "type": "http", "method": "GET", "path": "/x",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "query_string": b"",
    }
    req = Request(scope)
    claims = asyncio.get_event_loop().run_until_complete(require_jwt(req))
    assert claims["sub"] == "user-123"
    assert getattr(req.state, "jwt", None) == claims


def test_jwt_audience_mismatch_rejected():
    pytest.importorskip("jwt")
    import asyncio

    import jwt as _jwt
    from fastapi import HTTPException, Request

    from api.jwt_auth import require_jwt
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    rt.set("auth.jwt_enabled", True, actor="test")
    rt.set("auth.jwt_secret", "test-secret", actor="test")
    rt.set("auth.jwt_audience", "expected-aud", actor="test")

    token = _jwt.encode(
        {"sub": "user-123", "aud": "different-aud", "exp": 9999999999},
        "test-secret",
        algorithm="HS256",
    )
    scope = {
        "type": "http", "method": "GET", "path": "/x",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "query_string": b"",
    }
    req = Request(scope)
    with pytest.raises(HTTPException) as exc:
        asyncio.get_event_loop().run_until_complete(require_jwt(req))
    assert exc.value.status_code == 401
