"""Tests for the production security hardening:
- API key auth (with and without configured key)
- Security headers
- Request ID middleware
- Standardized error envelope
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app


# ---------------------------------------------------------------------------
# Helpers — set runtime / bootstrap config without env vars
# ---------------------------------------------------------------------------
def _set_runtime(key: str, value):
    """Write a runtime setting through the DB-backed store."""
    from src.runtime_settings import get_runtime
    get_runtime().set(key, value, actor="test")


def _patch_bootstrap_env(env: str):
    """Override the bootstrap `env` attribute on the cached Settings."""
    from src.settings import get_settings
    s = get_settings()
    return patch.object(s.app, "env", env)


# ---------------------------------------------------------------------------
# API key auth (driven by `auth.api_key` runtime setting)
# ---------------------------------------------------------------------------
def test_auth_disabled_by_default():
    """Empty `auth.api_key` runtime setting -> all endpoints accessible."""
    _set_runtime("auth.api_key", "")
    with TestClient(app) as c:
        assert c.get("/api/v1/summary").status_code == 200


def test_auth_required_when_key_set():
    _set_runtime("auth.api_key", "secret-test-key")
    try:
        with TestClient(app) as c:
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
    finally:
        _set_runtime("auth.api_key", "")


def test_health_does_not_require_auth():
    """Health probes must work without auth — load balancers depend on it."""
    _set_runtime("auth.api_key", "secret-test-key")
    try:
        with TestClient(app) as c:
            r = c.get("/api/health")
            assert r.status_code == 200
    finally:
        _set_runtime("auth.api_key", "")


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
def test_security_headers_present():
    with TestClient(app) as c:
        r = c.get("/api/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in r.headers
    assert "Content-Security-Policy" in r.headers


def test_hsts_only_in_prod():
    """HSTS should NOT be asserted in dev (we may not have TLS)."""
    with TestClient(app) as c:
        r = c.get("/api/health")
    assert "Strict-Transport-Security" not in r.headers


def test_hsts_present_in_prod(tmp_path, monkeypatch):
    """Write a temporary bootstrap.toml with env='prod' and rebuild the app.

    SecurityHeadersMiddleware reads `settings.is_prod` at app construction
    time — exercise it by pointing the bootstrap loader at a fresh file with
    prod set, reload api.main, then make a request.
    """
    import importlib

    from src import settings as settings_mod

    boot_path = tmp_path / "bootstrap.toml"
    boot_path.write_text(
        '[app]\n'
        'env = "prod"\n'
        'log_level = "INFO"\n'
        'log_format = "text"\n'
        '[database]\n'
        'url = "sqlite:///./data/admin.db"\n'
        '[admin]\n'
        'initial_password = "changeme-on-first-login"\n'
        'session_secret = "test-session-secret"\n'
    )
    monkeypatch.setattr(settings_mod, "DEFAULT_BOOTSTRAP_FILE", boot_path)
    settings_mod.get_settings.cache_clear()

    import api.main
    importlib.reload(api.main)
    try:
        with TestClient(api.main.app) as c:
            r = c.get("/api/health")
            assert "Strict-Transport-Security" in r.headers
    finally:
        # Reset to the real bootstrap so subsequent tests aren't affected
        monkeypatch.undo()
        settings_mod.get_settings.cache_clear()
        importlib.reload(api.main)


# ---------------------------------------------------------------------------
# Request ID middleware
# ---------------------------------------------------------------------------
def test_request_id_minted_when_absent():
    with TestClient(app) as c:
        r = c.get("/api/health")
    assert "X-Request-ID" in r.headers
    assert len(r.headers["X-Request-ID"]) >= 16  # uuid hex


def test_inbound_request_id_honored():
    """If a load balancer / mesh sends a request ID, we use it."""
    with TestClient(app) as c:
        r = c.get("/api/health", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers["X-Request-ID"] == "trace-abc-123"


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------
def test_404_uses_error_envelope():
    with TestClient(app) as c:
        r = c.get("/api/v1/meetings/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert "error" in body
    err = body["error"]
    assert err["code"] == "not_found"
    assert err["path"] == "/api/v1/meetings/does-not-exist"
    assert err["request_id"]


def test_validation_error_includes_field_details():
    """Trigger a validation error by sending an invalid query param."""
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
    with TestClient(app) as c:
        r = c.get("/api/health")
    assert any(h.lower().startswith("x-ratelimit") for h in r.headers)
