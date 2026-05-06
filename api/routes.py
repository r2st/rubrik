"""API routes — one endpoint per analytical surface.

All routes live under `/api/v1/...`. The unversioned `/api/...` paths are
preserved as a deprecated alias (registered in `api/main.py`) so existing
clients keep working through the v1 transition.

Auth gates the whole router: every endpoint depends on `require_api_key`,
which is a no-op when `Settings.api_key` is unset (dev convenience).
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from src import sentiment as sent_mod

from .auth import require_api_key
from .caching import cached
from .models import (
    ActionItemOwner,
    ClusterInfo,
    ClustersResponse,
    CompetitiveResponse,
    CustomerDetail,
    CustomerHealth,
    HealthResponse,
    IncidentImpactResponse,
    MeetingDetail,
    MeetingSummary,
    NegativePivot,
    SentimentByGroup,
    SummaryResponse,
    WeeklyTrendPoint,
)
from .state import get_state

# Public router (no auth) — for things load balancers / probes need to hit.
public_router = APIRouter(prefix="/api")

# Versioned router. Every route inherits the auth dependency.
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _meeting_to_summary(row: pd.Series) -> MeetingSummary:
    return MeetingSummary(
        meeting_id=row["meeting_id"],
        title=row["title"],
        call_type=row["call_type"],
        meeting_purpose=row["meeting_purpose"],
        primary_product=row["primary_product"],
        customer=row.get("customer"),
        start_time=row["start_time"].isoformat() if pd.notna(row["start_time"]) else "",
        duration_min=float(row["duration_min"] or 0),
        sentiment_score=float(row["sentiment_score"] or 0),
        overall_sentiment=row.get("overall_sentiment", ""),
        num_action_items=int(row["num_action_items"] or 0),
        max_drop=float(row["max_drop"]) if pd.notna(row.get("max_drop")) else None,
        share_negative=float(row["share_negative"]) if pd.notna(row.get("share_negative")) else None,
    )


def _filter(df: pd.DataFrame,
            call_types: Optional[list[str]] = None,
            products: Optional[list[str]] = None,
            date_from: Optional[date] = None,
            date_to: Optional[date] = None) -> pd.DataFrame:
    out = df
    if call_types:
        out = out[out["call_type"].isin(call_types)]
    if products:
        out = out[out["product_areas"].apply(lambda areas: bool(set(areas) & set(products)))]
    if date_from:
        out = out[out["start_time"].dt.date >= date_from]
    if date_to:
        out = out[out["start_time"].dt.date <= date_to]
    return out


# ---------------------------------------------------------------------------
# Health & summary
# ---------------------------------------------------------------------------
@public_router.get("/health", response_model=HealthResponse, tags=["meta"])
@public_router.get("/v1/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """Liveness probe. No auth required."""
    return HealthResponse()


@router.get("/summary", response_model=SummaryResponse, tags=["meta"])
def summary(request: Request, response: Response) -> SummaryResponse:
    s = get_state()
    df = s.df
    products: Counter[str] = Counter()
    for areas in df["product_areas"]:
        for p in areas:
            products[p] += 1
    body = SummaryResponse(
        n_meetings=len(df),
        date_range=s.metadata["date_range"],
        call_types=df["call_type"].value_counts().to_dict(),
        purposes=df["meeting_purpose"].value_counts().to_dict(),
        products=dict(products),
        sentiment={
            "overall": round(float(df["sentiment_score"].mean()), 2),
            **{ct: round(float(df[df["call_type"] == ct]["sentiment_score"].mean()), 2)
               for ct in df["call_type"].unique()},
        },
        n_clusters=s.metadata["n_clusters"],
        silhouette=s.metadata["silhouette"],
    )
    return cached(request, response, body)


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------
@router.get("/meetings", response_model=list[MeetingSummary], tags=["meetings"])
def list_meetings(
    call_type: Optional[list[str]] = Query(None),
    product: Optional[list[str]] = Query(None),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = Query(500, ge=1, le=1000),
) -> list[MeetingSummary]:
    df = _filter(get_state().df, call_type, product, date_from, date_to)
    df = df.sort_values("start_time", ascending=False).head(limit)
    return [_meeting_to_summary(r) for _, r in df.iterrows()]


@router.get("/meetings/{meeting_id}", response_model=MeetingDetail, tags=["meetings"])
def get_meeting(meeting_id: str) -> MeetingDetail:
    s = get_state()
    rows = s.df[s.df["meeting_id"] == meeting_id]
    if rows.empty:
        raise HTTPException(404, f"meeting {meeting_id} not found")
    row = rows.iloc[0]

    sentences = s.sentences_df[s.sentences_df["meeting_id"] == meeting_id].sort_values("index")
    speakers = s.speakers_df[s.speakers_df["meeting_id"] == meeting_id]

    base = _meeting_to_summary(row).model_dump()
    return MeetingDetail(
        **base,
        summary_text=row["summary_text"],
        topics=list(row["topics"]),
        product_areas=list(row["product_areas"]),
        action_items=list(row["action_items"]),
        trajectory=list(row["trajectory"]) if isinstance(row.get("trajectory"), list) else None,
        sentences=sentences[["index", "speaker", "sentence", "sentiment", "time"]].to_dict("records"),
        speakers=speakers[["speaker", "start_ts", "end_ts", "duration"]].to_dict("records"),
    )


# ---------------------------------------------------------------------------
# Sentiment views
# ---------------------------------------------------------------------------
@router.get("/sentiment/by-call-type", response_model=list[SentimentByGroup], tags=["sentiment"])
def sentiment_by_call_type() -> list[SentimentByGroup]:
    df = get_state().df
    return [
        SentimentByGroup(group=ct, mean=round(float(g["sentiment_score"].mean()), 2),
                         count=len(g))
        for ct, g in df.groupby("call_type")
    ]


@router.get("/sentiment/by-purpose", response_model=list[SentimentByGroup], tags=["sentiment"])
def sentiment_by_purpose() -> list[SentimentByGroup]:
    df = get_state().df
    rows = (df.groupby("meeting_purpose")["sentiment_score"]
            .agg(["mean", "count"]).reset_index().sort_values("mean"))
    return [
        SentimentByGroup(group=r["meeting_purpose"], mean=round(r["mean"], 2),
                         count=int(r["count"]))
        for _, r in rows.iterrows()
    ]


@router.get("/sentiment/weekly", response_model=list[WeeklyTrendPoint], tags=["sentiment"])
def sentiment_weekly() -> list[WeeklyTrendPoint]:
    df = get_state().df
    weekly = sent_mod.weekly_trend(df)
    return [
        WeeklyTrendPoint(week=int(r["week"]), call_type=r["call_type"],
                         sentiment_score=round(r["sentiment_score"], 2))
        for _, r in weekly.iterrows()
    ]


@router.get("/sentiment/scores", tags=["sentiment"])
def sentiment_scores() -> dict[str, list[float]]:
    """Raw scores per call type — for client-side boxplots."""
    df = get_state().df
    return {ct: g["sentiment_score"].tolist() for ct, g in df.groupby("call_type")}


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------
@router.get("/clusters", response_model=ClustersResponse, tags=["clusters"])
def clusters(request: Request, response: Response) -> ClustersResponse:
    s = get_state()
    cr = s.cluster_result
    items = []
    for cid, terms in cr.cluster_terms.items():
        members = s.df[s.df["content_cluster"] == cid]
        items.append(ClusterInfo(
            cluster=cid,
            size=len(members),
            top_terms=terms,
            dominant_purpose=members["meeting_purpose"].value_counts().index[0]
                if len(members) else "",
            avg_sentiment=round(float(members["sentiment_score"].mean()), 2)
                if len(members) else 0,
        ))
    body = ClustersResponse(k=cr.n_clusters, silhouette=round(cr.silhouette, 3),
                            clusters=items)
    return cached(request, response, body)


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------
@router.get("/insights/customer-health", response_model=list[CustomerHealth], tags=["insights"])
def customer_health() -> list[CustomerHealth]:
    h = get_state().health
    return [
        CustomerHealth(
            customer=r["customer"],
            risk_tier=r["risk_tier"],
            risk_score=float(r["risk_score"]),
            avg_sentiment=round(float(r["avg_sentiment"]), 2),
            min_sentiment=round(float(r["min_sentiment"]), 2),
            num_meetings=int(r["num_meetings"]),
            churn_signals=int(r["churn_signals"]),
        )
        for _, r in h.iterrows()
    ]


@router.get("/insights/customer/{customer}", response_model=CustomerDetail, tags=["insights"])
def customer_detail(customer: str) -> CustomerDetail:
    s = get_state()
    h = s.health[s.health["customer"] == customer]
    if h.empty:
        raise HTTPException(404, f"customer {customer} not found")
    meetings = s.df[s.df["customer"] == customer].sort_values("start_time")
    row = h.iloc[0]
    return CustomerDetail(
        customer=customer,
        risk_tier=row["risk_tier"],
        risk_score=float(row["risk_score"]),
        meetings=[_meeting_to_summary(r) for _, r in meetings.iterrows()],
    )


@router.get("/insights/incident-impact", response_model=IncidentImpactResponse, tags=["insights"])
def incident_impact() -> IncidentImpactResponse:
    inc = get_state().incident
    direct = inc["direct_incident_meetings"]
    return IncidentImpactResponse(
        n_total=inc["n_total"], n_affected=inc["n_affected"], n_direct=inc["n_direct"],
        affected_pct=inc["affected_pct"],
        sentiment_affected=inc["sentiment_affected"],
        sentiment_unaffected=inc["sentiment_unaffected"],
        by_call_type=inc["by_call_type"],
        direct_meetings=[_meeting_to_summary(r) for _, r in direct.iterrows()],
    )


@router.get("/insights/action-items", response_model=list[ActionItemOwner], tags=["insights"])
def action_items() -> list[ActionItemOwner]:
    ai = get_state().ai_load
    return [
        ActionItemOwner(owner=r["owner"], total=int(r["total"]),
                        external=int(r["external"]), internal=int(r["internal"]),
                        support=int(r["support"]))
        for _, r in ai.iterrows()
    ]


@router.get("/insights/competitive", response_model=CompetitiveResponse, tags=["insights"])
def competitive_signals() -> CompetitiveResponse:
    c = get_state().competitive
    flagged = c["flagged_meetings"].head(20)
    return CompetitiveResponse(
        n_flagged=c["n_flagged"], by_call_type=c["by_call_type"],
        sentiment_flagged=c["sentiment_flagged"],
        sentiment_other=c["sentiment_other"],
        sample_meetings=[_meeting_to_summary(r) for _, r in flagged.iterrows()],
    )


@router.get("/insights/negative-pivots", response_model=list[NegativePivot], tags=["insights"])
def negative_pivots() -> list[NegativePivot]:
    p = get_state().pivots
    return [
        NegativePivot(meeting_id=r["meeting_id"], title=r["title"],
                      call_type=r["call_type"],
                      sentiment_score=round(float(r["sentiment_score"]), 2),
                      max_drop=round(float(r["max_drop"]), 2),
                      share_negative=round(float(r["share_negative"]), 2))
        for _, r in p.iterrows()
    ]


@router.get("/insights/speaker-dominance", tags=["insights"])
def speaker_dominance() -> dict:
    d = get_state().dominance
    return {
        "by_call_type": d.groupby("call_type")["max_speaker_share"].mean().round(3).to_dict(),
        "dominated_count": int((d["max_speaker_share"] > 0.6).sum()),
        "points": [
            {"meeting_id": r["meeting_id"], "title": r["title"],
             "call_type": r["call_type"],
             "max_share": round(float(r["max_speaker_share"]), 3),
             "sentiment_score": round(float(r["sentiment_score"]), 2)}
            for _, r in d.iterrows()
        ],
    }
