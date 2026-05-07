"""End-to-end tests for the admin panel API.

Covers the auth flow (login → session cookie → require_admin), settings CRUD
(list, get, update, reset), the audit log, and the password rotation endpoint.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from src.runtime_settings import get_runtime

BOOTSTRAP_PASSWORD = "changeme-on-first-login"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def authed_client(client):
    """A TestClient with a valid admin session cookie set."""
    r = client.post("/api/v1/admin/login", json={"password": BOOTSTRAP_PASSWORD})
    assert r.status_code == 200
    return client


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_login_with_correct_password(client):
    r = client.post("/api/v1/admin/login", json={"password": BOOTSTRAP_PASSWORD})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "actor": "admin"}
    assert "ti_admin_session" in r.cookies


def test_login_with_wrong_password_returns_401(client):
    r = client.post("/api/v1/admin/login", json={"password": "wrong"})
    assert r.status_code == 401


def test_protected_endpoints_reject_unauth(client):
    for path in ["/api/v1/admin/me", "/api/v1/admin/settings",
                 "/api/v1/admin/audit"]:
        r = client.get(path)
        assert r.status_code == 401, f"{path} should require auth"


def test_me_returns_actor_when_authed(authed_client):
    r = authed_client.get("/api/v1/admin/me")
    assert r.status_code == 200
    assert r.json()["actor"] == "admin"


def test_logout_clears_session(authed_client):
    authed_client.post("/api/v1/admin/logout")
    # After logout, /me should reject
    r = authed_client.get("/api/v1/admin/me")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def test_list_settings_returns_categorized(authed_client):
    cats = authed_client.get("/api/v1/admin/settings").json()
    names = [c["category"] for c in cats]
    # The seeded categories from runtime_settings.DEFAULTS
    for expected in ["auth", "rate_limit", "risk", "feature"]:
        assert expected in names, f"missing category {expected}"
    # Internal categories must NOT leak through
    assert "_internal" not in names


def test_list_settings_includes_metadata(authed_client):
    cats = authed_client.get("/api/v1/admin/settings").json()
    auth_cat = next(c for c in cats if c["category"] == "auth")
    api_key_setting = next(s for s in auth_cat["settings"] if s["key"] == "auth.api_key")
    assert api_key_setting["type"] == "str"
    assert api_key_setting["description"]
    assert "updated_at" in api_key_setting


def test_update_setting_persists_and_appears_in_audit(authed_client):
    # Change rate_limit.default
    r = authed_client.put(
        "/api/v1/admin/settings/rate_limit.default",
        json={"value": "999/minute", "notes": "test bump"},
    )
    assert r.status_code == 200
    assert r.json()["value"] == "999/minute"

    # Confirm audit log captured it
    audit = authed_client.get("/api/v1/admin/audit?limit=10").json()
    matches = [e for e in audit if e["setting_key"] == "rate_limit.default"
               and e["new_value"] == "999/minute"]
    assert matches, "no audit entry for the rate_limit.default change"
    entry = matches[0]
    assert entry["actor"] == "admin"
    assert entry["action"] == "set"
    assert entry["notes"] == "test bump"

    # Reset for subsequent tests
    authed_client.post("/api/v1/admin/settings/rate_limit.default/reset")


def test_update_setting_coerces_types(authed_client):
    """Submitting a string '0.6' to a float setting should coerce."""
    r = authed_client.put(
        "/api/v1/admin/settings/risk.threshold_high",
        json={"value": "0.6"},
    )
    assert r.status_code == 200
    assert r.json()["value"] == 0.6
    authed_client.post("/api/v1/admin/settings/risk.threshold_high/reset")


def test_update_unknown_setting_returns_404(authed_client):
    r = authed_client.put(
        "/api/v1/admin/settings/no.such.setting",
        json={"value": "anything"},
    )
    assert r.status_code == 404


def test_reset_setting_restores_default(authed_client):
    # Change then reset
    authed_client.put(
        "/api/v1/admin/settings/risk.weight_low_sentiment",
        json={"value": 0.99},
    )
    r = authed_client.post(
        "/api/v1/admin/settings/risk.weight_low_sentiment/reset",
    )
    assert r.status_code == 200
    # Default per DEFAULTS is 0.5
    assert r.json()["value"] == 0.5


# ---------------------------------------------------------------------------
# Effect on the actual API behavior
# ---------------------------------------------------------------------------
def test_runtime_api_key_change_takes_effect(authed_client):
    """Set auth.api_key via admin → /api/v1/* requires it on next request."""
    try:
        # Enable auth via admin API
        authed_client.put(
            "/api/v1/admin/settings/auth.api_key",
            json={"value": "magic-test-token"},
        )

        # Without the key, request should now be unauthorized
        with TestClient(app) as c:
            r = c.get("/api/v1/summary")
            assert r.status_code == 401

            # With the key, request should pass
            r = c.get("/api/v1/summary",
                      headers={"X-API-Key": "magic-test-token"})
            assert r.status_code == 200
    finally:
        get_runtime().set("auth.api_key", "", actor="test-cleanup")


def test_audit_log_pagination(authed_client):
    r = authed_client.get("/api/v1/admin/audit?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) <= 5


# ---------------------------------------------------------------------------
# Password rotation
# ---------------------------------------------------------------------------
def test_change_password_rejects_wrong_current(authed_client):
    r = authed_client.post(
        "/api/v1/admin/password",
        json={"current_password": "wrong", "new_password": "longerthaneight"},
    )
    assert r.status_code == 400


def test_change_password_rejects_short_new(authed_client):
    r = authed_client.post(
        "/api/v1/admin/password",
        json={"current_password": BOOTSTRAP_PASSWORD, "new_password": "short"},
    )
    assert r.status_code == 422  # pydantic validation


def test_change_password_then_login_with_new(client):
    """Full rotation flow: login → change → login again with new password."""
    # Login first
    r = client.post("/api/v1/admin/login", json={"password": BOOTSTRAP_PASSWORD})
    assert r.status_code == 200

    # Rotate
    new_pw = "new-secret-2026"
    r = client.post(
        "/api/v1/admin/password",
        json={"current_password": BOOTSTRAP_PASSWORD, "new_password": new_pw},
    )
    assert r.status_code == 200

    # Old password fails on a fresh client
    with TestClient(app) as fresh:
        r = fresh.post("/api/v1/admin/login",
                       json={"password": BOOTSTRAP_PASSWORD})
        assert r.status_code == 401

        # New password succeeds
        r = fresh.post("/api/v1/admin/login", json={"password": new_pw})
        assert r.status_code == 200

    # Restore old password so other tests still work
    from api.admin.auth import update_admin_password
    update_admin_password(BOOTSTRAP_PASSWORD, actor="test-restore")
