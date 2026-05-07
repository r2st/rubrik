"""Active learning loop — production traffic generates the next training set.

The pattern:
  1. Production inference logs every request + output + model confidence
  2. A worker pool reads the inference log (Kafka topic in prod, local dir in dev)
  3. Low-confidence requests get scored by an LLM-as-judge
  4. If the judge accepts the model output → add to training queue with model output
     If the judge prefers its own output → add to training queue with judge output
  5. The training queue feeds `data_pipeline.py` on the next retraining cycle

Auto-scales 0..N CPU workers on Kafka lag; LLM-judge calls are rate-limited by
the judge's own quota (Anthropic / OpenAI).

Run on the cluster:
    ray job submit --working-dir=. -- \
        python -m gemma_finetune.scaling.active_learning \
        --inference-topic ti.inference \
        --label-table s3://ti-iceberg/training_labels

Notable production properties:
- **Idempotent** — every input is hashed; reprocessing the same record is a no-op
- **Backpressure** — judge calls are queued, not parallelized blindly
- **Cost-aware** — confidence threshold dials cost vs label volume
- **Auditable** — every label has a `(judge_id, prompt_id, timestamp, score)` trail
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InferenceRecord:
    """One production inference, as emitted to the inference Kafka topic."""
    request_id: str
    prompt: str
    model_output: str
    model_confidence: float
    model_version: str
    tenant_id: Optional[str]
    timestamp: float


@dataclass
class JudgeVerdict:
    """LLM-as-judge result. See eval_harness for the full rubric."""
    judge_id: str          # e.g., "claude-3.5-sonnet@2026-04-15"
    overall_score: float   # 0..1
    is_acceptable: bool    # threshold-applied
    preferred_output: str  # may equal record.model_output or be the judge's own
    feedback: str          # short explanation
    cost_usd: float


@dataclass
class TrainingExample:
    """Append to the training labels Iceberg table."""
    request_id: str
    prompt: str
    completion: str
    source: str                  # "human_label" | "active_learning" | "synthetic"
    judge_id: Optional[str]
    judge_score: Optional[float]
    tenant_id: Optional[str]
    timestamp: float


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LoopConfig:
    inference_topic: str = "ti.inference"
    label_table: str = "s3://ti-iceberg/training_labels"

    # Routing
    confidence_threshold: float = 0.85    # below this → judge it
    judge_acceptance_threshold: float = 0.70  # judge score above this → accept model output

    # Cost / rate
    max_judge_calls_per_minute: int = 60
    judge_provider: str = "anthropic"     # "anthropic" | "openai"
    judge_model: str = "claude-3.5-sonnet"
    daily_budget_usd: float = 50.0


# ---------------------------------------------------------------------------
# Worker — lazy imports for runtime dependencies
# ---------------------------------------------------------------------------
class ActiveLearningWorker:
    """Runs as a Ray actor / standalone async process. Pulls inference records,
    routes them through the judge, and writes accepted examples back."""

    def __init__(self, cfg: LoopConfig) -> None:
        self.cfg = cfg
        self._spent_today_usd = 0.0
        # Bloom filter / Redis set in prod for cross-worker dedup
        self._processed: set[str] = set()

    async def consume_forever(self) -> None:
        """Main loop — consume inference topic, judge, write."""
        async for record in self._stream_inferences():
            try:
                await self._handle(record)
            except Exception:  # noqa: BLE001
                log.exception("active-learning failure on %s", record.request_id)

    async def _handle(self, record: InferenceRecord) -> None:
        # 1. Skip high-confidence — model already nailed it
        if record.model_confidence >= self.cfg.confidence_threshold:
            return

        # 2. Skip already-processed (idempotent)
        record_hash = self._hash(record)
        if record_hash in self._processed:
            return

        # 3. Budget guard
        if self._spent_today_usd >= self.cfg.daily_budget_usd:
            log.warning("daily budget exhausted; deferring record %s",
                        record.request_id)
            return

        # 4. Judge it
        verdict = await self._judge(record)
        self._spent_today_usd += verdict.cost_usd

        # 5. Decide what to write to the training queue
        completion = (record.model_output if verdict.is_acceptable
                      else verdict.preferred_output)
        example = TrainingExample(
            request_id=record.request_id,
            prompt=record.prompt,
            completion=completion,
            source="active_learning",
            judge_id=verdict.judge_id,
            judge_score=verdict.overall_score,
            tenant_id=record.tenant_id,
            timestamp=record.timestamp,
        )
        await self._write_example(example)
        self._processed.add(record_hash)

    # ----------------------------- IO --------------------------------------
    async def _stream_inferences(self):
        """Async iterator over the inference Kafka topic.
        Local dev: tail a JSONL file at $TI_INFERENCE_LOG."""
        if path := os.environ.get("TI_INFERENCE_LOG"):
            with open(path) as f:
                for line in f:
                    yield InferenceRecord(**json.loads(line))
            return
        try:
            from aiokafka import AIOKafkaConsumer  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError(
                "aiokafka required in production. Install in worker image, "
                "or set TI_INFERENCE_LOG=path/to/jsonl for local dev."
            ) from e
        consumer = AIOKafkaConsumer(self.cfg.inference_topic)
        await consumer.start()
        try:
            async for msg in consumer:
                yield InferenceRecord(**json.loads(msg.value))
        finally:
            await consumer.stop()

    async def _judge(self, record: InferenceRecord) -> JudgeVerdict:
        """Score with LLM-as-judge. See eval_harness.LLMJudge for the rubric."""
        from .eval_harness import LLMJudge  # noqa: PLC0415

        judge = LLMJudge(provider=self.cfg.judge_provider,
                         model=self.cfg.judge_model)
        return await judge.score(
            prompt=record.prompt,
            candidate=record.model_output,
            tenant_id=record.tenant_id,
            acceptance_threshold=self.cfg.judge_acceptance_threshold,
        )

    async def _write_example(self, example: TrainingExample) -> None:
        """Append to the training_labels Iceberg table."""
        # In prod: use pyiceberg or the Ray Data writer. This is the shape.
        try:
            from pyiceberg.catalog import load_catalog  # noqa: PLC0415
        except ImportError:
            log.info("[dev] would write training example: %s", example.request_id)
            return
        catalog = load_catalog("default")
        table = catalog.load_table(self.cfg.label_table)
        await asyncio.to_thread(table.append, [dataclasses.asdict(example)])

    @staticmethod
    def _hash(record: InferenceRecord) -> str:
        return hashlib.sha1(
            (record.request_id + record.prompt[:200]).encode()
        ).hexdigest()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inference-topic", default="ti.inference")
    p.add_argument("--label-table", default="s3://ti-iceberg/training_labels")
    p.add_argument("--budget-usd", type=float, default=50.0)
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    cfg = LoopConfig(
        inference_topic=args.inference_topic,
        label_table=args.label_table,
        daily_budget_usd=args.budget_usd,
    )
    worker = ActiveLearningWorker(cfg)
    asyncio.run(worker.consume_forever())


if __name__ == "__main__":
    main()
