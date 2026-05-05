"""End-to-end API tests using TestClient (no network needed)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_summary_shape(client) -> None:
    r = client.get("/api/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["n_meetings"] == 100
    assert "external" in body["call_types"]
    assert "Detect" in body["products"]


def test_meetings_filtered_by_call_type(client) -> None:
    r = client.get("/api/meetings", params={"call_type": "support"})
    assert r.status_code == 200
    assert all(m["call_type"] == "support" for m in r.json())


def test_meetings_filtered_by_product(client) -> None:
    r = client.get("/api/meetings", params={"product": "Identity"})
    assert r.status_code == 200
    assert len(r.json()) > 0


def test_meeting_detail_has_sentences_and_trajectory(client) -> None:
    listing = client.get("/api/meetings", params={"limit": 1}).json()
    assert listing
    mid = listing[0]["meeting_id"]
    r = client.get(f"/api/meetings/{mid}")
    assert r.status_code == 200
    body = r.json()
    assert body["meeting_id"] == mid
    assert len(body["sentences"]) > 0
    assert all("sentiment" in s for s in body["sentences"])


def test_meeting_detail_404_for_unknown(client) -> None:
    r = client.get("/api/meetings/does-not-exist")
    assert r.status_code == 404


def test_clusters_endpoint(client) -> None:
    r = client.get("/api/clusters")
    assert r.status_code == 200
    body = r.json()
    assert body["k"] >= 4
    assert len(body["clusters"]) == body["k"]


def test_customer_health_returns_ranked_list(client) -> None:
    r = client.get("/api/insights/customer-health")
    assert r.status_code == 200
    body = r.json()
    scores = [c["risk_score"] for c in body]
    assert scores == sorted(scores, reverse=True)


def test_customer_detail_drill_down(client) -> None:
    r = client.get("/api/insights/customer/Northstar Pharma")
    assert r.status_code == 200
    body = r.json()
    assert body["customer"] == "Northstar Pharma"
    assert len(body["meetings"]) >= 1


def test_customer_detail_404(client) -> None:
    r = client.get("/api/insights/customer/Nope%20Inc")
    assert r.status_code == 404


def test_incident_impact(client) -> None:
    r = client.get("/api/insights/incident-impact")
    assert r.status_code == 200
    body = r.json()
    assert body["n_total"] == 100
    assert body["n_affected"] > 0
    assert body["sentiment_affected"] < body["sentiment_unaffected"]


def test_action_items(client) -> None:
    r = client.get("/api/insights/action-items")
    assert r.status_code == 200
    body = r.json()
    assert len(body) > 0
    assert all(o["total"] == o["external"] + o["internal"] + o["support"] for o in body)


def test_negative_pivots(client) -> None:
    r = client.get("/api/insights/negative-pivots")
    assert r.status_code == 200
    body = r.json()
    assert all(p["max_drop"] <= -0.5 for p in body)


def test_static_index_served(client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Transcript Intelligence" in r.text
