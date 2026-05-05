"""Tests for the insight modules."""
from __future__ import annotations

import pandas as pd

from src import insights


def test_customer_health_returns_known_at_risk(meetings_df) -> None:
    h = insights.customer_health(meetings_df)
    assert not h.empty
    # Required columns
    expected = {"customer", "avg_sentiment", "num_meetings",
                "churn_signals", "risk_score", "risk_tier"}
    assert expected.issubset(h.columns)
    # Risk scores in [0, 1]
    assert (h["risk_score"] >= 0).all() and (h["risk_score"] <= 1).all()
    # Sorted by risk descending
    assert h["risk_score"].is_monotonic_decreasing
    # Northstar Pharma is the documented top-risk customer in this dataset
    assert "Northstar Pharma" in h.head(3)["customer"].tolist()


def test_customer_health_tier_assignments_consistent(meetings_df) -> None:
    h = insights.customer_health(meetings_df)
    high = h[h["risk_tier"] == "🔴 high"]
    low = h[h["risk_tier"] == "🟢 low"]
    if len(high) and len(low):
        assert high["risk_score"].min() > low["risk_score"].max()


def test_incident_impact_quantifies_blast_radius(meetings_df) -> None:
    inc = insights.incident_impact(meetings_df)
    assert inc["n_total"] == 100
    assert inc["n_affected"] > 0
    assert inc["n_direct"] > 0
    # The outage should drag sentiment down
    assert inc["sentiment_affected"] < inc["sentiment_unaffected"]
    # Direct meetings (matched on title) should mostly overlap with affected
    # (matched on transcript content) — not a strict subset, since some titled
    # meetings don't repeat the keyword in their body.
    direct_ids = set(inc["direct_incident_meetings"]["meeting_id"])
    affected_ids = set(inc["affected_meetings"]["meeting_id"])
    overlap = len(direct_ids & affected_ids) / len(direct_ids)
    assert overlap >= 0.8, f"only {overlap:.0%} of direct meetings appear in affected"


def test_incident_impact_reasonable_for_known_outage(meetings_df) -> None:
    inc = insights.incident_impact(meetings_df)
    # The Detect outage is well-known to touch a majority of meetings
    assert inc["affected_pct"] >= 50
    # All call types should be affected
    assert {"external", "internal", "support"}.issubset(inc["by_call_type"].keys())


def test_action_item_load_returns_top_owners(meetings_df) -> None:
    ai = insights.action_item_load(meetings_df, top_n=10)
    assert len(ai) <= 10
    assert {"owner", "total", "external", "internal", "support"}.issubset(ai.columns)
    # Sorted descending
    assert ai["total"].is_monotonic_decreasing
    # Per-channel counts should sum to total
    sums = ai["external"] + ai["internal"] + ai["support"]
    assert (sums == ai["total"]).all()


def test_competitive_signals_returns_dict(meetings_df) -> None:
    c = insights.competitive_signals(meetings_df)
    assert {"flagged_meetings", "n_flagged", "by_call_type",
            "sentiment_flagged", "sentiment_other"}.issubset(c.keys())
    assert c["n_flagged"] >= 0


def test_speaker_dominance_shape(meetings_df, speakers_df) -> None:
    d = insights.speaker_dominance(speakers_df, meetings_df)
    assert {"meeting_id", "max_speaker_share", "call_type", "sentiment_score"}.issubset(d.columns)
    assert (d["max_speaker_share"] > 0).all()
    assert (d["max_speaker_share"] <= 1).all()


def test_negative_pivots_returns_meetings_below_threshold(meetings_df) -> None:
    p = insights.negative_pivots(meetings_df, threshold=-0.5)
    assert (p["max_drop"] <= -0.5).all()
    # Sorted ascending (sharpest drop first)
    assert p["max_drop"].is_monotonic_increasing


def test_negative_pivots_handles_missing_column() -> None:
    df = pd.DataFrame({"meeting_id": ["a"], "title": ["t"]})  # no max_drop
    out = insights.negative_pivots(df)
    assert out.empty
