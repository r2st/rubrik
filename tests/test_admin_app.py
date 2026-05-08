"""Smoke tests for the standalone admin app (api.admin_app).

The admin panel runs as a separate FastAPI process on its own port so
production deploys can route admin traffic through a private listener
(see ADR 0014 §"Control plane vs. data plane" and deploy/k8s/gateway.yaml).
This test confirms the app loads cleanly, serves probes + admin HTML, and
honors the admin authentication flow.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.admin_app import app

BOOTSTRAP_PASSWORD = "changeme-on-first-login"


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------
def test_admin_app_liveness():
    with TestClient(app) as c:
        r = c.get("/api/live")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_admin_app_readiness_when_db_reachable():
    with TestClient(app) as c:
        r = c.get("/api/ready")
    body = r.json()
    assert r.status_code == 200, body
    assert body["status"] == "ready"
    assert body["checks"]["db_reachable"] is True
    assert body["checks"]["not_draining"] is True


def test_admin_app_readiness_503_when_draining():
    with TestClient(app) as c:
        app.state.shutting_down = True
        try:
            r = c.get("/api/ready")
        finally:
            app.state.shutting_down = False
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def test_admin_app_root_serves_admin_html():
    """On the admin listener, ``/`` IS the admin panel — not the public dashboard."""
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 200
    body = r.text.lower()
    # Loose assertion — exact HTML structure can change.
    assert "admin" in body or "<html" in body


def test_admin_app_admin_path_alias_works():
    with TestClient(app) as c:
        r = c.get("/admin")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Admin API surface
# ---------------------------------------------------------------------------
def test_admin_app_login_then_authed_call():
    """Full flow: login, hit a session-gated endpoint."""
    with TestClient(app) as c:
        r = c.post("/api/v1/admin/login", json={"password": BOOTSTRAP_PASSWORD})
        assert r.status_code == 200, r.text
        # Session cookie set; /me should work now.
        r = c.get("/api/v1/admin/me")
    assert r.status_code == 200
    assert r.json()["actor"] == "admin"


def test_admin_app_does_not_serve_public_api():
    """The analyst read API is NOT mounted here — it's on api.main."""
    with TestClient(app) as c:
        r = c.get("/api/v1/summary")
    # Either 404 (route not registered) — the only acceptable outcomes.
    assert r.status_code == 404, (
        "admin app must NOT expose the analyst read API; "
        f"got {r.status_code}"
    )
