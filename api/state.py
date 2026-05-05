"""Pipeline state — runs the analysis once at startup, caches the result.

The dataset is static, so we trade memory for response latency. On a real
multi-instance deployment, swap this for a shared cache (Redis) or run the
pipeline as a batch job and serve from a persisted store.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src import categorizer, clustering, data_loader, insights, sentiment
from src.logging_config import get_logger

log = get_logger(__name__)


@dataclass
class PipelineState:
    df: pd.DataFrame
    sentences_df: pd.DataFrame
    speakers_df: pd.DataFrame
    cluster_result: Any
    health: pd.DataFrame
    incident: dict[str, Any]
    ai_load: pd.DataFrame
    competitive: dict[str, Any]
    dominance: pd.DataFrame
    pivots: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)


_state: PipelineState | None = None
_lock = threading.Lock()


def get_state() -> PipelineState:
    """Return the cached pipeline state, building it on first call."""
    global _state
    if _state is None:
        with _lock:
            if _state is None:
                _state = _build()
    return _state


def reload() -> PipelineState:
    """Force a reload — useful for tests or after data refresh."""
    global _state
    with _lock:
        _state = _build()
    return _state


def _build() -> PipelineState:
    log.info("Building pipeline state…")
    raw = data_loader.load_all_meetings()
    df = data_loader.meetings_to_dataframe(raw)
    sentences_df = data_loader.sentences_dataframe(raw)
    speakers_df = data_loader.speakers_dataframe(raw)

    df = categorizer.annotate(df)
    df["num_action_items"] = df["action_items"].apply(len)
    df = sentiment.add_trajectories(df, sentences_df)

    cluster_result = clustering.cluster_transcripts(df["full_transcript"])
    df["content_cluster"] = cluster_result.labels

    health = insights.customer_health(df)
    incident = insights.incident_impact(df)
    ai_load = insights.action_item_load(df, top_n=15)
    competitive = insights.competitive_signals(df)
    dominance = insights.speaker_dominance(speakers_df, df)
    pivots = insights.negative_pivots(df)

    log.info(
        "Pipeline ready: %d meetings, k=%d (silhouette=%.3f), %d at-risk customers",
        len(df), cluster_result.n_clusters, cluster_result.silhouette,
        len(health[health["risk_tier"] == "🔴 high"]) if len(health) else 0,
    )

    return PipelineState(
        df=df,
        sentences_df=sentences_df,
        speakers_df=speakers_df,
        cluster_result=cluster_result,
        health=health,
        incident=incident,
        ai_load=ai_load,
        competitive=competitive,
        dominance=dominance,
        pivots=pivots,
        metadata={
            "n_meetings": len(df),
            "date_range": [str(df["start_time"].min().date()),
                           str(df["start_time"].max().date())],
            "n_clusters": cluster_result.n_clusters,
            "silhouette": round(cluster_result.silhouette, 3),
        },
    )
