"""Strategic insights derived from the categorized + sentiment-annotated data.

Each insight is its own pure function returning a DataFrame, so the notebook
can render them independently and downstream tools can consume them.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

import pandas as pd

from . import config


def _runtime_risk_params() -> tuple[dict[str, float], dict[str, float]]:
    """Read risk weights + thresholds from the DB-backed runtime store.

    Tolerates absent DB (notebook / batch use) by falling back to the static
    config values.
    """
    try:
        from .runtime_settings import get_runtime
        rs = get_runtime()
        weights = {
            "low_sentiment": float(rs.get("risk.weight_low_sentiment", config.RISK_WEIGHTS["low_sentiment"])),
            "churn_signals": float(rs.get("risk.weight_churn_signals", config.RISK_WEIGHTS["churn_signals"])),
            "negative_pivots": float(rs.get("risk.weight_negative_pivots", config.RISK_WEIGHTS["negative_pivots"])),
        }
        thresholds = {
            "high": float(rs.get("risk.threshold_high", config.CHURN_RISK_THRESHOLDS["high"])),
            "medium": float(rs.get("risk.threshold_medium", config.CHURN_RISK_THRESHOLDS["medium"])),
        }
        return weights, thresholds
    except Exception:  # noqa: BLE001
        return dict(config.RISK_WEIGHTS), dict(config.CHURN_RISK_THRESHOLDS)


# ---------------------------------------------------------------------------
# Insight 1: Customer churn risk scoring
# ---------------------------------------------------------------------------
def _count_key_moments(moments: list[dict[str, Any]], moment_type: str) -> int:
    return sum(1 for km in (moments or []) if km.get("type") == moment_type)


def customer_health(meetings: pd.DataFrame) -> pd.DataFrame:
    """Aggregate customer-facing meetings into a churn-risk ranking.

    Risk score combines low sentiment, explicit churn signals, and within-meeting
    negative pivots, weighted per `config.RISK_WEIGHTS`. Returned scores are in [0, 1]
    where higher = more at risk.
    """
    cust = meetings[meetings["customer"].notna()].copy()
    if cust.empty:
        return pd.DataFrame()

    cust["churn_signal_count"] = cust["key_moments"].apply(
        lambda km: _count_key_moments(km, "churn_signal"))

    grouped = cust.groupby("customer").agg(
        avg_sentiment=("sentiment_score", "mean"),
        min_sentiment=("sentiment_score", "min"),
        num_meetings=("meeting_id", "count"),
        churn_signals=("churn_signal_count", "sum"),
        avg_max_drop=("max_drop", "mean"),
    ).reset_index()

    # Normalize each signal to [0, 1]
    sentiment_gap = (3.0 - grouped["avg_sentiment"]).clip(lower=0) / 2.0  # 1..3 → 1..0
    churn_norm = (grouped["churn_signals"] / max(grouped["churn_signals"].max(), 1)).clip(0, 1)
    pivot_norm = (-grouped["avg_max_drop"].fillna(0)).clip(lower=0)  # already in [0, 2]
    pivot_norm = (pivot_norm / 2.0).clip(0, 1)

    # Weights + thresholds are admin-tunable at runtime (see /admin).
    # Falls back to the static config if the runtime store isn't initialized
    # (e.g., when called from the notebook or batch pipeline).
    weights, thresholds = _runtime_risk_params()
    grouped["risk_score"] = (
        weights["low_sentiment"] * sentiment_gap
        + weights["churn_signals"] * churn_norm
        + weights["negative_pivots"] * pivot_norm
    ).round(3)

    def tier(score: float) -> str:
        if score >= thresholds["high"]:
            return "🔴 high"
        if score >= thresholds["medium"]:
            return "🟡 medium"
        return "🟢 low"

    grouped["risk_tier"] = grouped["risk_score"].apply(tier)
    return grouped.sort_values("risk_score", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Insight 2: Incident impact propagation
# ---------------------------------------------------------------------------
def incident_impact(
    meetings: pd.DataFrame,
    keywords: tuple[str, ...] = ("outage", "incident"),
) -> dict[str, Any]:
    """Quantify how an incident propagates through unrelated meetings."""
    pattern = "|".join(keywords)
    affected = meetings[meetings["full_transcript"].str.contains(pattern, case=False, na=False)]
    unaffected = meetings[~meetings.index.isin(affected.index)]
    direct = meetings[meetings["meeting_purpose"] == "Incident Response"].sort_values("start_time")

    return {
        "direct_incident_meetings": direct,
        "affected_meetings": affected,
        "n_total": len(meetings),
        "n_affected": len(affected),
        "n_direct": len(direct),
        "affected_pct": round(len(affected) / max(len(meetings), 1) * 100, 1),
        "sentiment_affected": round(affected["sentiment_score"].mean(), 2) if len(affected) else 0,
        "sentiment_unaffected": round(unaffected["sentiment_score"].mean(), 2) if len(unaffected) else 0,
        "by_call_type": affected["call_type"].value_counts().to_dict(),
    }


# ---------------------------------------------------------------------------
# Insight 3: Action item load distribution
# ---------------------------------------------------------------------------
def action_item_load(meetings: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    """Tabulate action item ownership and surface potential bottlenecks."""
    by_owner: Counter[str] = Counter()
    by_owner_calltype: dict[str, Counter[str]] = {}
    for _, row in meetings.iterrows():
        for ai in row["action_items"]:
            owner = ai.split(":", 1)[0].strip() if ":" in ai else "Unassigned"
            by_owner[owner] += 1
            by_owner_calltype.setdefault(owner, Counter())[row["call_type"]] += 1

    rows = []
    for owner, count in by_owner.most_common(top_n):
        breakdown = by_owner_calltype[owner]
        rows.append({
            "owner": owner,
            "total": count,
            "external": breakdown.get("external", 0),
            "internal": breakdown.get("internal", 0),
            "support": breakdown.get("support", 0),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Insight 4: Competitive language detection
# ---------------------------------------------------------------------------
def competitive_signals(meetings: pd.DataFrame) -> dict[str, Any]:
    """Find meetings where customers are evaluating alternatives."""
    pattern = "|".join(config.COMPETITIVE_KEYWORDS)
    flagged = meetings[meetings["full_transcript"].str.contains(pattern, case=False, na=False)]
    other = meetings[~meetings.index.isin(flagged.index)]
    return {
        "flagged_meetings": flagged,
        "n_flagged": len(flagged),
        "by_call_type": flagged["call_type"].value_counts().to_dict(),
        "sentiment_flagged": round(flagged["sentiment_score"].mean(), 2) if len(flagged) else 0,
        "sentiment_other": round(other["sentiment_score"].mean(), 2) if len(other) else 0,
    }


# ---------------------------------------------------------------------------
# Insight 5: Speaker dominance — meeting health
# ---------------------------------------------------------------------------
def speaker_dominance(speakers_df: pd.DataFrame, meetings: pd.DataFrame) -> pd.DataFrame:
    """For each meeting, compute the max single-speaker talk-time share."""
    talk = (speakers_df.groupby(["meeting_id", "speaker"])["duration"].sum()
            .reset_index())
    totals = talk.groupby("meeting_id")["duration"].sum().rename("total")
    talk = talk.join(totals, on="meeting_id")
    talk["share"] = talk["duration"] / talk["total"]
    max_share = (talk.groupby("meeting_id")["share"].max()
                 .reset_index()
                 .rename(columns={"share": "max_speaker_share"}))
    return max_share.merge(
        meetings[["meeting_id", "title", "call_type", "sentiment_score"]],
        on="meeting_id",
    )


# ---------------------------------------------------------------------------
# NEW Insight 6: Negative pivot detection (uses sentence-level trajectory)
# ---------------------------------------------------------------------------
def negative_pivots(meetings: pd.DataFrame, threshold: float = -0.5) -> pd.DataFrame:
    """Meetings with sharp within-call sentiment drops — friction moments worth reviewing."""
    if "max_drop" not in meetings.columns:
        return pd.DataFrame()
    pivots = meetings[meetings["max_drop"] <= threshold].copy()
    return pivots[["meeting_id", "title", "call_type", "sentiment_score",
                   "max_drop", "share_negative"]].sort_values("max_drop")
