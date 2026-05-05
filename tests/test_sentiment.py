"""Tests for sentiment trajectory math."""
from __future__ import annotations

import pandas as pd
import pytest

from src import sentiment


def _sentences(labels: list[str]) -> pd.DataFrame:
    return pd.DataFrame([
        {"index": i, "sentiment": lab} for i, lab in enumerate(labels)
    ])


def test_numeric_sentiment_mapping() -> None:
    assert sentiment.numeric_sentiment("positive") == 1.0
    assert sentiment.numeric_sentiment("neutral") == 0.0
    assert sentiment.numeric_sentiment("negative") == -1.0
    assert sentiment.numeric_sentiment("unknown") == 0.0


def test_trajectory_all_neutral_is_flat() -> None:
    out = sentiment.meeting_sentiment_trajectory(_sentences(["neutral"] * 10))
    assert out["trajectory"] == [0.0] * 5
    assert out["max_drop"] == 0.0
    assert out["share_negative"] == 0.0


def test_trajectory_detects_negative_pivot() -> None:
    # Positive start, negative middle, neutral end
    labels = (["positive"] * 4 + ["negative"] * 4 + ["neutral"] * 2)
    out = sentiment.meeting_sentiment_trajectory(_sentences(labels))
    assert out["max_drop"] < -0.5  # sharp drop from positive to negative
    assert out["share_negative"] == pytest.approx(0.4)


def test_trajectory_recovery_signal() -> None:
    labels = ["negative"] * 5 + ["positive"] * 5
    out = sentiment.meeting_sentiment_trajectory(_sentences(labels))
    assert out["end_minus_start"] > 0  # recovered


def test_trajectory_empty_input_safe() -> None:
    out = sentiment.meeting_sentiment_trajectory(pd.DataFrame(columns=["index", "sentiment"]))
    assert out["trajectory"] == [0.0, 0.0, 0.0, 0.0, 0.0]
    assert out["max_drop"] == 0.0


def test_add_trajectories_attaches_columns(meetings_df, sentences_df) -> None:
    # meetings_df fixture already has trajectories — verify columns exist
    for col in ("trajectory", "max_drop", "end_minus_start", "share_negative"):
        assert col in meetings_df.columns
    # All trajectories should be lists of length 5
    assert all(isinstance(t, list) and len(t) == 5 for t in meetings_df["trajectory"])


def test_summary_by_group_returns_expected_columns(meetings_df) -> None:
    s = sentiment.summary_by_group(meetings_df, "call_type")
    assert {"mean", "std", "min", "max", "count"}.issubset(s.columns)
    assert set(s.index) == {"external", "internal", "support"}


def test_weekly_trend_has_call_type_and_week(meetings_df) -> None:
    w = sentiment.weekly_trend(meetings_df)
    assert {"week", "call_type", "sentiment_score"} == set(w.columns)
    assert w["week"].dtype.kind == "i"
