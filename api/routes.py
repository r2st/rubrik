"""API routes — one endpoint per analytical surface.

All routes live under `/api/v1/...`. The unversioned `/api/...` paths are
preserved as a deprecated alias (registered in `api/main.py`) so existing
clients keep working through the v1 transition.

Auth gates the whole router: every endpoint depends on `require_api_key`,
which is a no-op when `Settings.api_key` is unset (dev convenience).
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from src import sentiment as sent_mod

from . import state
from .auth import require_api_key
from .caching import cached
from .limiter import per_tenant_rate_limit_dep
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
    """Combined health probe (kept for backward compatibility).

    Prefer ``/api/live`` for k8s liveness and ``/api/ready`` for k8s
    readiness — they have distinct semantics.
    """
    return HealthResponse()


@public_router.get("/live", tags=["meta"])
def liveness() -> dict:
    """Liveness probe — process is up and the event loop is responsive.

    Cheap on purpose: no DB call, no pipeline check. k8s should restart the
    pod only if this fails.
    """
    return {"status": "alive"}


@public_router.get("/ready", tags=["meta"])
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe — true iff this replica can serve real traffic.

    Returns 200 only when:
      - the process is not in shutdown drain (``app.state.shutting_down``)
      - the pipeline cache is warm (``state.is_warm()``)
      - the admin DB is reachable (settings table is queryable)

    Otherwise returns 503 so the LB drops the replica out of rotation. A
    flap during refresh briefly drains the replica, which is correct.
    """
    from .backpressure import current_inflight, current_rejected
    from .circuit_breaker import CircuitOpenError, all_breakers, get_breaker

    draining = bool(getattr(request.app.state, "shutting_down", False))
    state_ok = state.is_warm()

    # The DB probe is wrapped in a circuit breaker — if Postgres is sick we
    # stop hammering it from every replica's readiness loop and degrade fast.
    db_breaker = get_breaker("readiness_db_probe", failure_threshold=3,
                             recovery_timeout_s=10.0)

    async def _probe_db():
        from src.db import session_scope
        from src.models_db import Setting

        def _sync():
            with session_scope() as s:
                s.query(Setting).limit(1).all()
        await asyncio.to_thread(_sync)

    db_ok = True
    try:
        # Hard 2 s deadline on the DB probe — we never want a slow
        # Postgres to make readiness itself slow. Timeout = unhealthy.
        await asyncio.wait_for(db_breaker.call(_probe_db), timeout=2.0)
    except (CircuitOpenError, asyncio.TimeoutError, Exception):  # noqa: BLE001
        db_ok = False

    # Redis probe — only when configured. Reflects the health of the
    # cluster-wide rate limiter + Arq queue + idempotency cache. If the
    # operator set redis_url but Redis is unhealthy, those subsystems
    # silently degrade to in-process — the LB should drop us out of
    # rotation so we don't serve degraded responses.
    redis_ok = True
    redis_configured = False
    try:
        from src.settings import get_settings
        url = get_settings().redis_url
        if url:
            redis_configured = True

            def _ping():
                import redis
                redis.Redis.from_url(url, socket_timeout=0.25).ping()
            # Outer wait_for guards against socket-timeout being honored
            # but the connection-pool acquire stalling. Belt-and-braces.
            await asyncio.wait_for(asyncio.to_thread(_ping), timeout=1.0)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        redis_ok = False

    breakers = {n: b.state.value for n, b in all_breakers().items()}
    ready = state_ok and db_ok and not draining and (
        redis_ok if redis_configured else True
    )
    checks = {
        "pipeline_warm": state_ok,
        "db_reachable": db_ok,
        "not_draining": not draining,
    }
    if redis_configured:
        checks["redis_reachable"] = redis_ok
    body = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "inflight": current_inflight(),
        "rejected_total": current_rejected(),
        "circuit_breakers": breakers,
    }
    return JSONResponse(status_code=(200 if ready else 503), content=body)


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
@router.get(
    "/meetings",
    response_model=list[MeetingSummary],
    tags=["meetings"],
    # Per-tenant cap override. The global limiter already gives fairness via
    # tenant_aware_key (each tenant has their own bucket); this dependency
    # additionally enforces a per-tenant cap from rate_limit.per_tenant when
    # the operator has set one, returning 429 + Retry-After.
    dependencies=[Depends(per_tenant_rate_limit_dep)],
)
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
