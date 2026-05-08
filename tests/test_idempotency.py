"""Tests for the Idempotency-Key middleware (ADR 0014, research §"Backpressure must be explicit")."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.runtime_settings import get_runtime


@pytest.fixture(autouse=True)
def _enable_idempotency():
    rt = get_runtime()
    rt.set("idempotency.enabled", True, actor="test")
    yield
    rt.set("idempotency.enabled", False, actor="test")


# ---------------------------------------------------------------------------
# Disabled by default — header is honoured only when the flag is on
# ---------------------------------------------------------------------------
def test_idempotency_off_by_default():
    """The flag is off by default; the header has no effect."""
    rt = get_runtime()
    rt.set("idempotency.enabled", False, actor="test")
    from api.main import app
    with TestClient(app) as c:
        r1 = c.post("/api/v1/admin/login",
                    json={"password": "wrong"},
                    headers={"Idempotency-Key": "k-off-1"})
        r2 = c.post("/api/v1/admin/login",
                    json={"password": "wrong"},
                    headers={"Idempotency-Key": "k-off-1"})
    # Both requests reach the handler — no idempotency replay.
    assert r1.status_code in (401, 429)
    assert r2.status_code in (401, 429)
    assert "Idempotency-Status" not in r1.headers
    assert "Idempotency-Status" not in r2.headers


# ---------------------------------------------------------------------------
# Cache hit / miss flow
# ---------------------------------------------------------------------------
def test_first_request_marked_stored():
    from api.main import app
    with TestClient(app) as c:
        r = c.post("/api/v1/admin/login",
                   json={"password": "wrong"},
                   headers={"Idempotency-Key": "k-store-1"})
    assert r.headers.get("Idempotency-Status") == "stored"


def test_second_request_replays_cached_response():
    from api.main import app
    with TestClient(app) as c:
        r1 = c.post("/api/v1/admin/login",
                    json={"password": "wrong"},
                    headers={"Idempotency-Key": "k-replay-1"})
        r2 = c.post("/api/v1/admin/login",
                    json={"password": "wrong"},
                    headers={"Idempotency-Key": "k-replay-1"})
    assert r1.headers.get("Idempotency-Status") == "stored"
    assert r2.headers.get("Idempotency-Status") == "replayed"
    # Bodies match exactly — same request_id, same status, same payload.
    assert r1.status_code == r2.status_code
    assert r1.text == r2.text


def test_same_key_different_body_returns_409():
    """Same Idempotency-Key with a different payload = client bug → 409."""
    from api.main import app
    with TestClient(app) as c:
        c.post("/api/v1/admin/login",
               json={"password": "wrong"},
               headers={"Idempotency-Key": "k-conflict-1"})
        r = c.post("/api/v1/admin/login",
                   json={"password": "different"},
                   headers={"Idempotency-Key": "k-conflict-1"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"


# ---------------------------------------------------------------------------
# Bypass paths + non-state-changing methods
# ---------------------------------------------------------------------------
def test_get_requests_pass_through():
    """GET / HEAD aren't state-changing — never cached, never replayed."""
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/health", headers={"Idempotency-Key": "k-get"})
    assert "Idempotency-Status" not in r.headers


def test_health_probes_bypass_cache():
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/live", headers={"Idempotency-Key": "k-probe"})
    assert "Idempotency-Status" not in r.headers


def test_request_without_key_passes_through():
    from api.main import app
    with TestClient(app) as c:
        r = c.post("/api/v1/admin/login", json={"password": "wrong"})
    assert "Idempotency-Status" not in r.headers


# ---------------------------------------------------------------------------
# Admin app — same middleware applies to the separate process
# ---------------------------------------------------------------------------
def test_admin_app_replays_idempotent_post():
    """The admin app's password / settings / snapshot routes get the same protection."""
    from api.admin_app import app as admin_app
    with TestClient(admin_app) as c:
        r1 = c.post("/api/v1/admin/login",
                    json={"password": "wrong"},
                    headers={"Idempotency-Key": "admin-k-1"})
        r2 = c.post("/api/v1/admin/login",
                    json={"password": "wrong"},
                    headers={"Idempotency-Key": "admin-k-1"})
    assert r1.headers.get("Idempotency-Status") == "stored"
    assert r2.headers.get("Idempotency-Status") == "replayed"
