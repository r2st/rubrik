"""Tests for graceful-degradation behavior + OpenAPI examples.

Verifies:
- X-State-Age-Seconds is on every /api/* response
- X-Stale-Response is set when refresh has been failing past 2× the interval
- OpenAPI schema includes concrete examples on the major response models
- The /docs page is reachable
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import state as state_mod
from api.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# X-State-Age-Seconds — present on every /api/* response
# ---------------------------------------------------------------------------
def test_state_age_header_on_health(client) -> None:
    r = client.get("/api/health")
    assert "X-State-Age-Seconds" in r.headers
    assert r.headers["X-State-Age-Seconds"].isdigit()


def test_state_age_header_on_summary(client) -> None:
    r = client.get("/api/v1/summary")
    age = int(r.headers["X-State-Age-Seconds"])
    assert age >= 0
    assert age < 600  # state was just built; should be young


def test_state_age_not_on_static_routes(client) -> None:
    """Static assets and the dashboard page don't need the header."""
    r = client.get("/")
    assert "X-State-Age-Seconds" not in r.headers


# ---------------------------------------------------------------------------
# X-Stale-Response — only when refresh is misbehaving
# ---------------------------------------------------------------------------
def test_stale_header_absent_when_healthy(client) -> None:
    r = client.get("/api/v1/summary")
    assert r.headers.get("X-Stale-Response") is None


def test_stale_header_set_when_refresh_failing(client) -> None:
    """Simulate stale state by manipulating the module-level globals.

    This exercises the is_stale() logic without actually waiting hours for
    a refresh interval.
    """
    # Pretend refresh interval is 1 minute and we've had 5 consecutive failures
    # and the state is 10 minutes old.
    with patch.object(state_mod, "_refresh_interval_minutes", 1), \
         patch.object(state_mod, "_consecutive_refresh_failures", 5):
        # Backdate the state's built_at so age > 2 * interval
        original = state_mod._state.built_at_monotonic
        state_mod._state.built_at_monotonic = time.monotonic() - 600
        try:
            r = client.get("/api/v1/summary")
            assert r.headers.get("X-Stale-Response") == "true"
            age = int(r.headers["X-State-Age-Seconds"])
            assert age >= 600
        finally:
            state_mod._state.built_at_monotonic = original


def test_is_stale_false_when_refresh_disabled() -> None:
    """If refresh is disabled (interval=0), responses are never marked stale."""
    with patch.object(state_mod, "_refresh_interval_minutes", 0), \
         patch.object(state_mod, "_consecutive_refresh_failures", 99):
        assert state_mod.is_stale() is False


def test_is_stale_false_when_state_is_fresh() -> None:
    """Even if some refreshes failed, recent successful build = not stale."""
    with patch.object(state_mod, "_refresh_interval_minutes", 5), \
         patch.object(state_mod, "_consecutive_refresh_failures", 1):
        # Force a recent built_at
        original = state_mod._state.built_at_monotonic
        state_mod._state.built_at_monotonic = time.monotonic()
        try:
            assert state_mod.is_stale() is False
        finally:
            state_mod._state.built_at_monotonic = original


# ---------------------------------------------------------------------------
# OpenAPI examples
# ---------------------------------------------------------------------------
def test_openapi_schema_reachable(client) -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    body = r.json()
    assert body["info"]["title"] == "Transcript Intelligence API"


def test_openapi_summary_has_example(client) -> None:
    schema = client.get("/openapi.json").json()
    summary_schema = schema["components"]["schemas"]["SummaryResponse"]
    assert "example" in summary_schema, "SummaryResponse must have an example"
    ex = summary_schema["example"]
    assert ex["n_meetings"] == 100
    assert "external" in ex["call_types"]


def test_openapi_meeting_summary_has_example(client) -> None:
    schema = client.get("/openapi.json").json()
    ms = schema["components"]["schemas"]["MeetingSummary"]
    assert "example" in ms
    ex = ms["example"]
    assert ex["call_type"] in ("external", "internal", "support")
    assert ex.get("customer")  # the example uses Northstar Pharma


def test_openapi_customer_health_has_example(client) -> None:
    schema = client.get("/openapi.json").json()
    ch = schema["components"]["schemas"]["CustomerHealth"]
    assert "example" in ch
    ex = ch["example"]
    assert "risk_tier" in ex
    assert 0 <= ex["risk_score"] <= 1


def test_docs_page_reachable(client) -> None:
    r = client.get("/docs")
    assert r.status_code == 200
    assert "swagger" in r.text.lower() or "openapi" in r.text.lower()
