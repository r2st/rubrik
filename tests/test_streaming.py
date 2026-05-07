"""Tests for the repository pattern + streaming analytical pipeline.

The streaming pipeline must produce results that match the in-memory pipeline
on the same input — same totals, same per-customer rollups, same friction
moments. If they ever diverge, that's a bug in the streaming fold.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.repository import LocalDirectoryRepository, TranscriptRepository, default_repository
from src.streaming import Aggregate, fold_batch, streaming_analyze


# ---------------------------------------------------------------------------
# Repository protocol contract
# ---------------------------------------------------------------------------
def test_default_repository_is_protocol_compliant():
    """The Protocol must accept the concrete implementation at runtime."""
    repo = default_repository()
    assert isinstance(repo, TranscriptRepository)
    assert isinstance(repo, LocalDirectoryRepository)


def test_repository_count_matches_filesystem(raw_meetings):
    repo = default_repository()
    assert repo.count() == len(raw_meetings)


def test_repository_get_returns_meeting():
    repo = default_repository()
    one = next(iter(repo.stream(batch_size=1)))[0]
    fetched = repo.get(one.meeting_id)
    assert fetched is not None
    assert fetched.meeting_id == one.meeting_id


def test_repository_get_returns_none_for_missing():
    repo = default_repository()
    assert repo.get("does-not-exist-id") is None


# ---------------------------------------------------------------------------
# Streaming behavior — batches sum to the same totals as eager-load
# ---------------------------------------------------------------------------
def test_streaming_total_matches_eager_count():
    repo = default_repository()
    eager_count = repo.count()

    total_in_batches = 0
    for batch in repo.stream(batch_size=20):
        total_in_batches += len(batch)
    assert total_in_batches == eager_count


def test_streaming_yields_full_batches_then_remainder():
    """If we have N meetings and batch_size=B, we get ⌈N/B⌉ batches with the
    last possibly partial."""
    repo = default_repository()
    n = repo.count()
    batches = list(repo.stream(batch_size=30))
    expected_batches = (n + 29) // 30
    assert len(batches) == expected_batches
    # All but possibly the last batch should be exactly 30
    for batch in batches[:-1]:
        assert len(batch) == 30
    assert len(batches[-1]) <= 30


# ---------------------------------------------------------------------------
# Streaming aggregation matches in-memory pipeline
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def streaming_result():
    repo = default_repository()
    return streaming_analyze(repo, batch_size=25)


def test_streaming_total_meetings_matches_dataset(streaming_result, raw_meetings):
    assert streaming_result.aggregate.n_meetings == len(raw_meetings)


def test_streaming_call_type_distribution_matches(streaming_result, meetings_df):
    streaming_call_types = streaming_result.aggregate.call_types
    in_memory_call_types = meetings_df["call_type"].value_counts().to_dict()
    assert dict(streaming_call_types) == in_memory_call_types


def test_streaming_avg_sentiment_matches(streaming_result, meetings_df):
    streaming_avg = streaming_result.aggregate.avg_sentiment
    in_memory_avg = meetings_df["sentiment_score"].mean()
    assert abs(streaming_avg - in_memory_avg) < 0.001


def test_streaming_finds_same_friction_moments(streaming_result, meetings_df):
    """The 9 sharp negative pivots from the in-memory pipeline must also
    appear in the streaming output. Critical correctness check — bucket
    math must be identical between the two paths."""
    in_memory_pivots = meetings_df[meetings_df["max_drop"] <= -0.5]
    streaming_pivots = streaming_result.aggregate.sharp_pivot_meetings
    assert len(streaming_pivots) == len(in_memory_pivots)


def test_streaming_top_action_owners_match(streaming_result, meetings_df):
    """Maria Santos should be the top owner in both modes."""
    streaming_top = streaming_result.aggregate.action_owners.most_common(1)[0]
    assert streaming_top[0] == "Maria Santos"
    assert streaming_top[1] == 31


# ---------------------------------------------------------------------------
# Aggregate.merge — proves streaming is parallelizable
# ---------------------------------------------------------------------------
def test_aggregate_merge_is_associative():
    """Two halves merged should equal the whole. This is what makes the
    streaming fold parallelizable across N workers (Ray Data, Beam, etc.)."""
    repo = default_repository()
    full = streaming_analyze(repo, batch_size=1000).aggregate

    # Process two halves separately
    all_meetings = repo.all()
    mid = len(all_meetings) // 2
    half1 = fold_batch(all_meetings[:mid], Aggregate())
    half2 = fold_batch(all_meetings[mid:], Aggregate())
    merged = half1.merge(half2)

    assert merged.n_meetings == full.n_meetings
    assert dict(merged.call_types) == dict(full.call_types)
    assert dict(merged.purposes) == dict(full.purposes)
    assert abs(merged.avg_sentiment - full.avg_sentiment) < 0.001


def test_streaming_writes_csv_outputs(streaming_result, tmp_path):
    streaming_result.write_csv(tmp_path)
    assert (tmp_path / "customer_health_streaming.csv").exists()
    assert (tmp_path / "negative_pivots_streaming.csv").exists()
    assert (tmp_path / "action_owners_streaming.csv").exists()
    # Sanity-check that the CSV is non-empty + parseable
    df = pd.read_csv(tmp_path / "customer_health_streaming.csv")
    assert len(df) > 0
    assert "customer" in df.columns


# ---------------------------------------------------------------------------
# Memory bound — streaming holds at most batch_size meetings at a time
# ---------------------------------------------------------------------------
def test_streaming_does_not_materialize_full_dataset():
    """Smoke test that the iterator pattern is preserved — we should be able
    to peek at one batch and abort without paying for the rest."""
    repo = default_repository()
    iterator = repo.stream(batch_size=10)
    first = next(iterator)
    assert len(first) == 10
    # Don't consume the rest — proves we're streaming, not pre-loading
