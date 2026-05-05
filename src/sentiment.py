"""Sentiment analysis at meeting and sentence granularity.

The dataset provides per-sentence `sentimentType` (positive/neutral/negative).
We use that to compute *trajectories* — within-meeting sentiment dynamics —
which surface signals that the meeting-level score alone hides (e.g., a call
that ends well but had a sharp negative pivot midway).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Map categorical labels to numeric scores for trajectory math.
SENT_NUMERIC = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}


def numeric_sentiment(label: str) -> float:
    return SENT_NUMERIC.get(label, 0.0)


def meeting_sentiment_trajectory(sentences: pd.DataFrame, n_buckets: int = 5) -> dict:
    """Bucket a meeting's sentences into N equal segments and average sentiment.

    Returns a dict with the trajectory and derived signals:
      - trajectory: list of N floats in [-1, 1]
      - max_drop: largest negative delta between consecutive buckets
      - end_minus_start: end - start sentiment (recovery signal)
      - share_negative: fraction of sentences labeled negative
    """
    if sentences.empty:
        return {"trajectory": [0.0] * n_buckets, "max_drop": 0.0,
                "end_minus_start": 0.0, "share_negative": 0.0}

    sorted_s = sentences.sort_values("index").reset_index(drop=True)
    sorted_s["score"] = sorted_s["sentiment"].map(SENT_NUMERIC).fillna(0.0)
    bucket_idx = np.linspace(0, n_buckets, len(sorted_s) + 1)[:-1].astype(int)
    sorted_s["bucket"] = np.clip(bucket_idx, 0, n_buckets - 1)
    trajectory = sorted_s.groupby("bucket")["score"].mean().reindex(
        range(n_buckets), fill_value=0.0
    ).tolist()

    deltas = np.diff(trajectory)
    max_drop = float(deltas.min()) if len(deltas) > 0 else 0.0
    return {
        "trajectory": trajectory,
        "max_drop": max_drop,
        "end_minus_start": trajectory[-1] - trajectory[0],
        "share_negative": float((sorted_s["sentiment"] == "negative").mean()),
    }


def add_trajectories(meetings_df: pd.DataFrame, sentences_df: pd.DataFrame) -> pd.DataFrame:
    """Join trajectory features onto the meeting DataFrame."""
    feats = []
    for mid, group in sentences_df.groupby("meeting_id"):
        traj = meeting_sentiment_trajectory(group)
        traj["meeting_id"] = mid
        feats.append(traj)
    feat_df = pd.DataFrame(feats)
    return meetings_df.merge(feat_df, on="meeting_id", how="left")


def summary_by_group(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    """Compact sentiment summary table grouped by any column."""
    return (
        df.groupby(group_col)["sentiment_score"]
        .agg(["mean", "std", "min", "max", "count"])
        .round(2)
        .sort_values("mean")
    )


def weekly_trend(df: pd.DataFrame) -> pd.DataFrame:
    """Mean sentiment per ISO week × call type."""
    out = df.copy()
    out["week"] = out["start_time"].dt.isocalendar().week.astype(int)
    return out.groupby(["week", "call_type"])["sentiment_score"].mean().reset_index()
