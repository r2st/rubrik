"""Tests for the production security hardening:
- API key auth (with and without configured key)
- Security headers
- Request ID middleware
- Standardized error envelope
"""
from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers — we need to reload the app to pick up env-var changes
# ---------------------------------------------------------------------------
def _client_with_settings(monkeypatch, **env):
    """Spin up a fresh app instance with overridden settings."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Bust caches so settings + app rebuild
    from src import settings as settings_mod
    settings_mod.get_settings.cache_clear()
    import api.main
    importlib.reload(api.main)
    return TestClient(api.main.app)


# ---------------------------------------------------------------------------
# API key auth
# ---------------------------------------------------------------------------
def test_auth_disabled_by_default():
    """No API_KEY env -> all endpoints accessible."""
    from api.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/summary").status_code == 200


def test_auth_required_when_key_set(monkeypatch):
    with _client_with_settings(monkeypatch, API_KEY="secret-test-key") as c:
        # Missing key -> 401 with envelope
        r = c.get("/api/v1/summary")
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == "unauthorized"
        assert "missing or invalid" in body["error"]["message"].lower()

        # Wrong key -> 401
        r = c.get("/api/v1/summary", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

        # Correct key -> 200
        r = c.get("/api/v1/summary", headers={"X-API-Key": "secret-test-key"})
        assert r.status_code == 200


def test_health_does_not_require_auth(monkeypatch):
    """Health probes must work without auth — load balancers depend on it."""
    with _client_with_settings(monkeypatch, API_KEY="secret-test-key") as c:
        r = c.get("/api/health")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
def test_security_headers_present():
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in r.headers
    assert "Content-Security-Policy" in r.headers


def test_hsts_only_in_prod(monkeypatch):
    """HSTS should NOT be asserted in dev (we may not have TLS)."""
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/health")
    assert "Strict-Transport-Security" not in r.headers


def test_hsts_present_in_prod(monkeypatch):
    with _client_with_settings(monkeypatch, ENV="prod") as c:
        r = c.get("/api/health")
    assert "Strict-Transport-Security" in r.headers


# ---------------------------------------------------------------------------
# Request ID middleware
# ---------------------------------------------------------------------------
def test_request_id_minted_when_absent():
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/health")
    assert "X-Request-ID" in r.headers
    assert len(r.headers["X-Request-ID"]) >= 16  # uuid hex


def test_inbound_request_id_honored():
    """If a load balancer / mesh sends a request ID, we use it (don't mint a new one)."""
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/health", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers["X-Request-ID"] == "trace-abc-123"


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------
def test_404_uses_error_envelope():
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/v1/meetings/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert "error" in body
    err = body["error"]
    assert err["code"] == "not_found"
    assert err["path"] == "/api/v1/meetings/does-not-exist"
    assert err["request_id"]  # populated by middleware


def test_validation_error_includes_field_details():
    """Trigger a validation error by sending an invalid query param."""
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/v1/meetings", params={"limit": "not-an-int"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "validation_failed"
    assert body["error"]["details"]
    assert any("limit" in str(d.get("loc", [])) for d in body["error"]["details"])


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_rate_limit_headers_present():
    """slowapi exposes X-RateLimit-* headers when default limits are configured."""
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/health")
    # slowapi adds these when headers_enabled=True (we set this in main.py)
    assert any(h.lower().startswith("x-ratelimit") for h in r.headers)
