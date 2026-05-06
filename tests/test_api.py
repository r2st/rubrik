"""End-to-end API tests using TestClient (no network needed).

All routes live under /api/v1/. The unauthenticated /api/health is the only
exception. Auth is dependency-disabled by default (no API_KEY env), so these
tests don't supply X-API-Key.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Health & meta
# ---------------------------------------------------------------------------
def test_health_unauthenticated(client) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_v1_alias(client) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200


def test_summary_shape(client) -> None:
    r = client.get("/api/v1/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["n_meetings"] == 100
    assert "external" in body["call_types"]
    assert "Detect" in body["products"]


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------
def test_meetings_filtered_by_call_type(client) -> None:
    r = client.get("/api/v1/meetings", params={"call_type": "support"})
    assert r.status_code == 200
    assert all(m["call_type"] == "support" for m in r.json())


def test_meetings_filtered_by_product(client) -> None:
    r = client.get("/api/v1/meetings", params={"product": "Identity"})
    assert r.status_code == 200
    assert len(r.json()) > 0


def test_meeting_detail_has_sentences_and_trajectory(client) -> None:
    listing = client.get("/api/v1/meetings", params={"limit": 1}).json()
    assert listing
    mid = listing[0]["meeting_id"]
    r = client.get(f"/api/v1/meetings/{mid}")
    assert r.status_code == 200
    body = r.json()
    assert body["meeting_id"] == mid
    assert len(body["sentences"]) > 0
    assert all("sentiment" in s for s in body["sentences"])


def test_meeting_detail_404_for_unknown(client) -> None:
    r = client.get("/api/v1/meetings/does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Clusters & insights
# ---------------------------------------------------------------------------
def test_clusters_endpoint(client) -> None:
    r = client.get("/api/v1/clusters")
    assert r.status_code == 200
    body = r.json()
    assert body["k"] >= 4
    assert len(body["clusters"]) == body["k"]


def test_customer_health_returns_ranked_list(client) -> None:
    r = client.get("/api/v1/insights/customer-health")
    assert r.status_code == 200
    scores = [c["risk_score"] for c in r.json()]
    assert scores == sorted(scores, reverse=True)


def test_customer_detail_drill_down(client) -> None:
    r = client.get("/api/v1/insights/customer/Northstar Pharma")
    assert r.status_code == 200
    body = r.json()
    assert body["customer"] == "Northstar Pharma"
    assert len(body["meetings"]) >= 1


def test_customer_detail_404(client) -> None:
    r = client.get("/api/v1/insights/customer/Nope%20Inc")
    assert r.status_code == 404


def test_incident_impact(client) -> None:
    r = client.get("/api/v1/insights/incident-impact")
    assert r.status_code == 200
    body = r.json()
    assert body["n_total"] == 100
    assert body["sentiment_affected"] < body["sentiment_unaffected"]


def test_action_items(client) -> None:
    r = client.get("/api/v1/insights/action-items")
    assert r.status_code == 200
    body = r.json()
    assert all(o["total"] == o["external"] + o["internal"] + o["support"] for o in body)


def test_negative_pivots(client) -> None:
    r = client.get("/api/v1/insights/negative-pivots")
    assert r.status_code == 200
    assert all(p["max_drop"] <= -0.5 for p in r.json())


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
def test_static_index_served(client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Transcript Intelligence" in r.text


def test_favicon_served(client) -> None:
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")


def test_legacy_unversioned_path_returns_404(client) -> None:
    """Routes are only at /api/v1/*. Unversioned /api/* should 404 cleanly."""
    r = client.get("/api/summary")
    assert r.status_code == 404


def test_metrics_endpoint(client) -> None:
    r = client.get("/metrics")
    # Endpoint exists when METRICS_ENABLED=true (default)
    assert r.status_code == 200
    assert "http_requests_total" in r.text or "process_" in r.text
