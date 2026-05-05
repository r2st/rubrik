"""Pydantic response models — explicit API contracts."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"


class SummaryResponse(BaseModel):
    n_meetings: int
    date_range: list[str]
    call_types: dict[str, int]
    purposes: dict[str, int]
    products: dict[str, int]
    sentiment: dict[str, float]
    n_clusters: int
    silhouette: float


class MeetingSummary(BaseModel):
    meeting_id: str
    title: str
    call_type: str
    meeting_purpose: str
    primary_product: str
    customer: Optional[str] = None
    start_time: str
    duration_min: float
    sentiment_score: float
    overall_sentiment: str
    num_action_items: int
    max_drop: Optional[float] = None
    share_negative: Optional[float] = None


class MeetingDetail(MeetingSummary):
    summary_text: str
    topics: list[str]
    product_areas: list[str]
    action_items: list[str]
    trajectory: Optional[list[float]] = None
    sentences: list[dict[str, Any]] = Field(default_factory=list)
    speakers: list[dict[str, Any]] = Field(default_factory=list)


class ClusterInfo(BaseModel):
    cluster: int
    size: int
    top_terms: list[str]
    dominant_purpose: str
    avg_sentiment: float


class ClustersResponse(BaseModel):
    k: int
    silhouette: float
    clusters: list[ClusterInfo]


class CustomerHealth(BaseModel):
    customer: str
    risk_tier: str
    risk_score: float
    avg_sentiment: float
    min_sentiment: float
    num_meetings: int
    churn_signals: int


class CustomerDetail(BaseModel):
    customer: str
    risk_tier: str
    risk_score: float
    meetings: list[MeetingSummary]


class IncidentImpactResponse(BaseModel):
    n_total: int
    n_affected: int
    n_direct: int
    affected_pct: float
    sentiment_affected: float
    sentiment_unaffected: float
    by_call_type: dict[str, int]
    direct_meetings: list[MeetingSummary]


class ActionItemOwner(BaseModel):
    owner: str
    total: int
    external: int
    internal: int
    support: int


class CompetitiveResponse(BaseModel):
    n_flagged: int
    by_call_type: dict[str, int]
    sentiment_flagged: float
    sentiment_other: float
    sample_meetings: list[MeetingSummary]


class SentimentByGroup(BaseModel):
    group: str
    mean: float
    count: int


class WeeklyTrendPoint(BaseModel):
    week: int
    call_type: str
    sentiment_score: float


class NegativePivot(BaseModel):
    meeting_id: str
    title: str
    call_type: str
    sentiment_score: float
    max_drop: float
    share_negative: float
