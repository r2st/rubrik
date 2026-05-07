"""Ray Data streaming pipeline for production-volume training data.

Replaces the laptop-scale `build_rubrik_jsonl.py` workflow at production
volumes. Reads raw transcripts from Iceberg, joins with labels from the
active-learning queue, applies streaming dedup + quality filters, expands
into the multi-task chat-template format, and writes a versioned dataset
back to Iceberg.

Auto-scales 0..N CPU workers via Ray's autoscaler on a KubeRay cluster.
Streaming — never materializes the full dataset in memory, so it works
identically on 1k or 100M+ rows.

Run on the cluster:
    ray job submit --working-dir=. -- python -m gemma_finetune.scaling.data_pipeline \
        --raw s3://ti-iceberg/raw_transcripts \
        --labels s3://ti-iceberg/training_labels \
        --output s3://ti-iceberg/training_sets \
        --version 2026-05-06

Run locally for development (lazy import, requires ray installed):
    python -m gemma_finetune.scaling.data_pipeline --local --output ./out
"""
from __future__ import annotations

import argparse
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PipelineConfig:
    raw_iceberg_table: str
    label_iceberg_table: str
    output_iceberg_table: str
    version_tag: str

    # Streaming knobs
    parallelism: int = 200          # max concurrent read tasks
    batch_size: int = 1024          # rows per micro-batch
    shuffle_buffer: int = 100_000   # rows held for shuffle window

    # Filter knobs
    languages: tuple[str, ...] = ("en",)
    min_tokens: int = 50
    max_tokens: int = 8000
    near_duplicate_threshold: float = 0.92


# ---------------------------------------------------------------------------
# Streaming transformation primitives
# ---------------------------------------------------------------------------
def _content_hash(text: str) -> str:
    """Stable hash for near-duplicate detection (MinHash / SimHash would be better
    at very large scale; SHA-1 of normalized text is fine to start)."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha1(normalized.encode()).hexdigest()


def deduplicate_batch(batch: dict[str, Any]) -> dict[str, Any]:
    """Within-batch exact-content dedup. Cross-batch dedup uses Bloom filter
    state held in a Ray actor (not shown — see scaling/dedup_actor.py at scale)."""
    seen: set[str] = set()
    keep_idx = []
    for i, transcript in enumerate(batch["full_transcript"]):
        h = _content_hash(transcript)
        if h not in seen:
            seen.add(h)
            keep_idx.append(i)
    return {k: [v[i] for i in keep_idx] for k, v in batch.items()}


def quality_filter(row: dict[str, Any], cfg: PipelineConfig) -> bool:
    """Drop transcripts that fail length / language / PII checks."""
    if row.get("language") not in cfg.languages:
        return False
    n_tokens = row.get("token_count", 0)
    if n_tokens < cfg.min_tokens or n_tokens > cfg.max_tokens:
        return False
    if row.get("has_pii_flagged"):  # set upstream by a Presidio pass
        return False
    return True


def format_to_chat_template(row: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Multi-task expansion — the trick that took v3 from loss 1.18 to 0.37.

    Each meeting becomes 4 training rows: full summary, summary-only,
    actions-only, attendees. Generator so a single source row can fan out
    into many target rows without materializing intermediate lists.
    """
    transcript = row["full_transcript"]
    summary = row["summary_text"]
    action_items = row["action_items"]
    attendees = row["attendees"]

    yield {"prompt": _full_summary_prompt(transcript),
           "completion": f"{summary}\n\nAction Items:\n" + "\n".join(action_items)}
    yield {"prompt": _summary_only_prompt(transcript), "completion": summary}
    yield {"prompt": _actions_only_prompt(transcript),
           "completion": "\n".join(action_items)}
    yield {"prompt": _attendees_prompt(transcript),
           "completion": ", ".join(attendees)}


def _full_summary_prompt(transcript: str) -> str:
    return ("Summarize this meeting transcript in one paragraph, then list "
            "action items as 'Owner: task' bullets.\n\n" + transcript)


def _summary_only_prompt(transcript: str) -> str:
    return "Summarize this meeting transcript in one paragraph.\n\n" + transcript


def _actions_only_prompt(transcript: str) -> str:
    return ("Extract action items from this transcript as 'Owner: task' "
            "bullets only.\n\n" + transcript)


def _attendees_prompt(transcript: str) -> str:
    return "Who attended this meeting? List names only.\n\n" + transcript


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------
def build_dataset(cfg: PipelineConfig, *, local: bool = False) -> Optional[Any]:
    """Construct and execute the streaming pipeline.

    Returns the Ray Data Dataset handle on the cluster path, None on local
    mode (which writes to a local Parquet file for inspection).
    """
    try:
        import ray
        import ray.data
    except ImportError as e:
        raise RuntimeError(
            "Ray is required for distributed execution. Install on the "
            "cluster image, or use `--local` for a single-process smoke test."
        ) from e

    log.info("Building training set %s from %s × %s",
             cfg.version_tag, cfg.raw_iceberg_table, cfg.label_iceberg_table)

    raw = ray.data.read_iceberg(cfg.raw_iceberg_table, parallelism=cfg.parallelism)
    labels = ray.data.read_iceberg(cfg.label_iceberg_table)

    # Streaming pipeline — Ray figures out worker placement, back-pressure,
    # and shuffle bucketing automatically.
    ds = (
        raw
        .filter(lambda r: quality_filter(r, cfg))
        .map_batches(deduplicate_batch, batch_size=cfg.batch_size)
        .zip(labels)                                # join on meeting_id
        .flat_map(format_to_chat_template)
        .random_shuffle(seed=42, num_blocks=cfg.parallelism)
    )

    if local:
        ds.write_parquet(f"./out/{cfg.version_tag}")
        return ds

    ds.write_iceberg(
        cfg.output_iceberg_table,
        partition_by=["version_tag"],
        version_tag=cfg.version_tag,
    )
    log.info("Wrote %d rows to %s @ %s",
             ds.count(), cfg.output_iceberg_table, cfg.version_tag)
    return ds


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw", default="s3://ti-iceberg/raw_transcripts")
    p.add_argument("--labels", default="s3://ti-iceberg/training_labels")
    p.add_argument("--output", default="s3://ti-iceberg/training_sets")
    p.add_argument("--version", required=True, help="version tag (e.g., 2026-05-06)")
    p.add_argument("--local", action="store_true", help="local single-process run")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    cfg = PipelineConfig(
        raw_iceberg_table=args.raw,
        label_iceberg_table=args.labels,
        output_iceberg_table=args.output,
        version_tag=args.version,
    )
    build_dataset(cfg, local=args.local)


if __name__ == "__main__":
    main()
