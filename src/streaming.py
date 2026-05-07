"""Streaming analytical pipeline for production-volume data.

The default pipeline (`run_analysis.py` without `--streaming`) holds the full
dataset in pandas — fine for the client sample, fails at production volume.

This module provides a streaming alternative: process meetings in batches,
fold partial results, never hold the full set in memory. Same insights, same
output schema, scales to whatever the repository can stream.

Usage:

    from src.repository import default_repository
    from src.streaming import streaming_analyze

    repo = default_repository()
    result = streaming_analyze(repo, batch_size=1000)
    print(result.summary)        # category counts, sentiment averages
    result.write_csv("./out/")   # same outputs as the in-memory pipeline

For sample volumes (≤ ~100k records), the in-memory pipeline is faster.
For production (≥ ~1M records), streaming is required.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from . import categorizer, sentiment
from .data_loader import Meeting, meetings_to_dataframe, sentences_dataframe
from .logging_config import get_logger
from .repository import TranscriptRepository

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Folded aggregate — built incrementally one batch at a time
# ---------------------------------------------------------------------------
@dataclass
class Aggregate:
    """Running totals across all batches processed so far.

    Designed to be **mergeable** — two `Aggregate` objects can be combined
    by adding their counts. That makes this trivially parallelizable: split
    the repository across N workers, each builds its own Aggregate, then
    reduce-merge into one. (See ADR 0010 for how this slots into Ray Data.)
    """
    n_meetings: int = 0
    n_sentences: int = 0
    call_types: Counter[str] = field(default_factory=Counter)
    purposes: Counter[str] = field(default_factory=Counter)
    products: Counter[str] = field(default_factory=Counter)

    # Running sums for averages
    sentiment_total: float = 0.0
    sentiment_count: int = 0
    sentiment_by_call_type: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    # Per-customer aggregation (only for external)
    customer_meetings: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    customer_churn_signals: Counter[str] = field(default_factory=Counter)

    # Within-call friction
    sharp_pivot_meetings: list[dict[str, Any]] = field(default_factory=list)

    # Action item ownership
    action_owners: Counter[str] = field(default_factory=Counter)

    def merge(self, other: Aggregate) -> Aggregate:
        """Combine two aggregates. Pure — returns a new Aggregate."""
        out = Aggregate(
            n_meetings=self.n_meetings + other.n_meetings,
            n_sentences=self.n_sentences + other.n_sentences,
            call_types=self.call_types + other.call_types,
            purposes=self.purposes + other.purposes,
            products=self.products + other.products,
            sentiment_total=self.sentiment_total + other.sentiment_total,
            sentiment_count=self.sentiment_count + other.sentiment_count,
            sharp_pivot_meetings=self.sharp_pivot_meetings + other.sharp_pivot_meetings,
            action_owners=self.action_owners + other.action_owners,
            customer_churn_signals=self.customer_churn_signals + other.customer_churn_signals,
        )
        # Defaultdicts need manual merge
        for k, v in self.sentiment_by_call_type.items():
            out.sentiment_by_call_type[k].extend(v)
        for k, v in other.sentiment_by_call_type.items():
            out.sentiment_by_call_type[k].extend(v)
        for k, v in self.customer_meetings.items():
            out.customer_meetings[k].extend(v)
        for k, v in other.customer_meetings.items():
            out.customer_meetings[k].extend(v)
        return out

    @property
    def avg_sentiment(self) -> float:
        return self.sentiment_total / max(self.sentiment_count, 1)


# ---------------------------------------------------------------------------
# Per-batch fold — runs the full categorizer/sentiment pipeline on a batch,
# then extracts everything needed into the running Aggregate
# ---------------------------------------------------------------------------
def fold_batch(batch: list[Meeting], agg: Aggregate) -> Aggregate:
    df = meetings_to_dataframe(batch)
    df = categorizer.annotate(df)
    df["num_action_items"] = df["action_items"].apply(len)
    sent_df = sentences_dataframe(batch)
    df = sentiment.add_trajectories(df, sent_df)

    agg.n_meetings += len(df)
    agg.n_sentences += len(sent_df)
    agg.call_types.update(df["call_type"].tolist())
    agg.purposes.update(df["meeting_purpose"].tolist())
    for products in df["product_areas"]:
        agg.products.update(products)

    sentiments = df["sentiment_score"].dropna().tolist()
    agg.sentiment_total += sum(sentiments)
    agg.sentiment_count += len(sentiments)
    for ct, score in zip(df["call_type"], df["sentiment_score"]):
        if pd.notna(score):
            agg.sentiment_by_call_type[ct].append(float(score))

    # Customer aggregation
    for _, row in df.iterrows():
        cust = row.get("customer")
        if cust:
            agg.customer_meetings[cust].append(float(row["sentiment_score"]))
            for km in row.get("key_moments", []) or []:
                if km.get("type") == "churn_signal":
                    agg.customer_churn_signals[cust] += 1

    # Friction moments
    pivots = df[df["max_drop"] <= -0.5] if "max_drop" in df.columns else df.iloc[0:0]
    for _, row in pivots.iterrows():
        agg.sharp_pivot_meetings.append({
            "meeting_id": row["meeting_id"],
            "title": row["title"],
            "max_drop": float(row["max_drop"]),
            "sentiment_score": float(row["sentiment_score"]),
        })

    # Action items
    for _, row in df.iterrows():
        for ai in row.get("action_items", []) or []:
            owner = ai.split(":", 1)[0].strip() if ":" in ai else "Unassigned"
            agg.action_owners[owner] += 1

    return agg


# ---------------------------------------------------------------------------
# Top-level streaming entry point
# ---------------------------------------------------------------------------
@dataclass
class StreamingResult:
    """Output of a streaming run. Mirrors the shape of the in-memory pipeline's
    summary so downstream consumers don't need to branch on which mode ran."""
    aggregate: Aggregate
    n_batches: int
    elapsed_s: float

    @property
    def summary(self) -> dict[str, Any]:
        a = self.aggregate
        return {
            "n_meetings": a.n_meetings,
            "n_sentences": a.n_sentences,
            "n_batches": self.n_batches,
            "elapsed_s": round(self.elapsed_s, 2),
            "rows_per_sec": round(a.n_meetings / max(self.elapsed_s, 0.001), 1),
            "call_types": dict(a.call_types),
            "purposes": dict(a.purposes.most_common()),
            "products": dict(a.products.most_common()),
            "avg_sentiment": round(a.avg_sentiment, 2),
            "sentiment_by_call_type": {
                ct: round(sum(s) / max(len(s), 1), 2)
                for ct, s in a.sentiment_by_call_type.items()
            },
            "n_customers": len(a.customer_meetings),
            "n_friction_moments": len(a.sharp_pivot_meetings),
            "top_action_owners": a.action_owners.most_common(10),
        }

    def write_csv(self, output_dir: Path) -> None:
        """Write streaming-mode equivalents of the in-memory pipeline's CSV outputs."""
        output_dir.mkdir(parents=True, exist_ok=True)
        a = self.aggregate

        # Customer health rollup
        rows = []
        for cust, scores in a.customer_meetings.items():
            rows.append({
                "customer": cust,
                "avg_sentiment": round(sum(scores) / len(scores), 2),
                "min_sentiment": round(min(scores), 2),
                "num_meetings": len(scores),
                "churn_signals": a.customer_churn_signals.get(cust, 0),
            })
        pd.DataFrame(rows).sort_values("avg_sentiment").to_csv(
            output_dir / "customer_health_streaming.csv", index=False)

        # Friction moments
        pd.DataFrame(a.sharp_pivot_meetings).to_csv(
            output_dir / "negative_pivots_streaming.csv", index=False)

        # Action item owners
        pd.DataFrame(a.action_owners.most_common(),
                     columns=["owner", "count"]).to_csv(
            output_dir / "action_owners_streaming.csv", index=False)


def streaming_analyze(
    repo: TranscriptRepository,
    *,
    batch_size: int = 1000,
    progress_every: int = 10,
) -> StreamingResult:
    """Run the analytical pipeline against a repository in streaming fashion.

    Memory usage is O(batch_size) regardless of the total dataset size.
    Repository implementations decide how meetings are sourced (filesystem,
    Postgres, Iceberg, Kafka — all interchangeable behind the Protocol).
    """
    import time
    log.info("Streaming analyze: total=%d, batch=%d", repo.count(), batch_size)
    start = time.monotonic()
    agg = Aggregate()
    n_batches = 0
    for batch in repo.stream(batch_size=batch_size):
        agg = fold_batch(batch, agg)
        n_batches += 1
        if n_batches % progress_every == 0:
            elapsed = time.monotonic() - start
            log.info("  processed %d meetings (%.0f rows/s)",
                     agg.n_meetings, agg.n_meetings / max(elapsed, 0.001))
    elapsed = time.monotonic() - start
    log.info("Streaming complete: %d meetings in %d batches, %.1fs",
             agg.n_meetings, n_batches, elapsed)
    return StreamingResult(aggregate=agg, n_batches=n_batches, elapsed_s=elapsed)
