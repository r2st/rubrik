"""Pydantic response models — explicit API contracts.

Each model includes a `json_schema_extra` example so the auto-generated
OpenAPI page at `/docs` shows real, copy-pasteable response payloads.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"status": "ok", "version": "1.0.0"}
    })
    status: str = "ok"
    version: str = "0.1.0"


class SummaryResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "n_meetings": 100,
            "date_range": ["2026-02-03", "2026-04-28"],
            "call_types": {"external": 43, "internal": 30, "support": 27},
            "purposes": {
                "Support Resolution": 27,
                "Account Management": 13,
                "Incident Response": 12,
            },
            "products": {"Detect": 70, "Comply": 67, "Protect": 33, "Identity": 23},
            "sentiment": {
                "overall": 3.42,
                "external": 3.71,
                "internal": 3.42,
                "support": 2.94,
            },
            "n_clusters": 7,
            "silhouette": 0.082,
        }
    })
    n_meetings: int
    date_range: list[str]
    call_types: dict[str, int]
    purposes: dict[str, int]
    products: dict[str, int]
    sentiment: dict[str, float]
    n_clusters: int
    silhouette: float


class MeetingSummary(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "meeting_id": "01KQ03B0303900521BB089CA",
            "title": "URGENT: Northstar Pharma - Detect Outage Impact",
            "call_type": "external",
            "meeting_purpose": "Incident Response",
            "primary_product": "Detect",
            "customer": "Northstar Pharma",
            "start_time": "2026-03-12T12:15:00",
            "duration_min": 35.2,
            "sentiment_score": 2.1,
            "overall_sentiment": "negative",
            "num_action_items": 5,
            "max_drop": -0.6,
            "share_negative": 0.31,
        }
    })
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
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "k": 7,
            "silhouette": 0.082,
            "clusters": [
                {
                    "cluster": 0,
                    "size": 4,
                    "top_terms": ["billing", "marcus", "account", "accounts", "v2", "tier"],
                    "dominant_purpose": "Support Resolution",
                    "avg_sentiment": 2.85,
                },
                {
                    "cluster": 3,
                    "size": 23,
                    "top_terms": ["failure", "event", "monitoring", "pipeline", "hours", "processing"],
                    "dominant_purpose": "Incident Response",
                    "avg_sentiment": 2.4,
                },
            ],
        }
    })
    k: int
    silhouette: float
    clusters: list[ClusterInfo]


class CustomerHealth(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "customer": "Northstar Pharma",
            "risk_tier": "🔴 high",
            "risk_score": 0.54,
            "avg_sentiment": 2.1,
            "min_sentiment": 2.1,
            "num_meetings": 1,
            "churn_signals": 3,
        }
    })
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
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "n_total": 100,
            "n_affected": 68,
            "n_direct": 12,
            "affected_pct": 68.0,
            "sentiment_affected": 2.85,
            "sentiment_unaffected": 3.62,
            "by_call_type": {"external": 31, "support": 22, "internal": 15},
            "direct_meetings": [],
        }
    })
    n_total: int
    n_affected: int
    n_direct: int
    affected_pct: float
    sentiment_affected: float
    sentiment_unaffected: float
    by_call_type: dict[str, int]
    direct_meetings: list[MeetingSummary]


class ActionItemOwner(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "owner": "Maria Santos",
            "total": 31,
            "external": 12,
            "internal": 14,
            "support": 5,
        }
    })
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
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "meeting_id": "01KQ1DC6CA536DE1B31ED8F5",
            "title": "URGENT: Blackridge Investments - Complete Loss of Threat Visibility",
            "call_type": "external",
            "sentiment_score": 1.6,
            "max_drop": -0.85,
            "share_negative": 0.42,
        }
    })
    meeting_id: str
    title: str
    call_type: str
    sentiment_score: float
    max_drop: float
    share_negative: float
