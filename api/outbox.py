"""Transactional outbox + relayer (ADR 0014).

The outbox is the cloud-agnostic answer to *"how do I update my OLTP and
emit an event without dual-writing?"*. The pattern:

  1. The application writes the entity change AND inserts an
     ``OutboxEvent`` row in the **same database transaction**. Either
     both land or neither does.
  2. A separate relayer process (this module) tails ``outbox_events``,
     publishes each row to the event backbone (Kafka / Kinesis / Event
     Hubs / NATS), and marks the row processed.
  3. Downstream consumers (cache invalidators, OLAP loaders, search
     indexers, downstream services) read from the event backbone and
     dedupe on ``(aggregate_id, sequence)``.

This module ships:
  - ``emit()`` — application-side helper for inserting outbox rows
    inside an existing SQLAlchemy session.
  - ``Relayer`` — the consumer side, with a pluggable ``Publisher``
    so deployments can wire it to whatever event backbone is in scope
    (the default ``InMemoryPublisher`` is for tests / dev).

Idempotency contract: the relayer marks a row processed *after* the
publisher acknowledges. A crash between publish and mark causes a
duplicate; consumers must tolerate that. Losing a row is not allowed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.db import session_scope
from src.logging_config import get_logger
from src.models_db import OutboxEvent

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Application-side: emit an event in the same transaction as the state change
# ---------------------------------------------------------------------------
def emit(
    session: Session,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict,
    sequence: int = 0,
) -> OutboxEvent:
    """Insert an OutboxEvent inside the caller's existing transaction.

    Caller is responsible for committing the surrounding transaction. If
    the commit fails, the event row is rolled back together with the
    state change — the whole point of the pattern.
    """
    row = OutboxEvent(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
        sequence=sequence,
    )
    session.add(row)
    return row


# ---------------------------------------------------------------------------
# Publisher protocol — implementers wire to Kafka/Kinesis/etc.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OutboxRecord:
    """Plain-dict view of an OutboxEvent for the publisher boundary."""
    id: int
    aggregate_type: str
    aggregate_id: str
    event_type: str
    sequence: int
    payload: Any


class Publisher(Protocol):
    def publish(self, event: OutboxRecord) -> None: ...


class InMemoryPublisher:
    """Default publisher — appends to an in-memory list. Useful for
    tests, dev, and verifying the relayer drains correctly without an
    external broker. Production deployments swap this for KafkaPublisher
    (or equivalent) at startup.
    """

    def __init__(self) -> None:
        self.delivered: list[OutboxRecord] = []

    def publish(self, event: OutboxRecord) -> None:
        self.delivered.append(event)


# ---------------------------------------------------------------------------
# Relayer
# ---------------------------------------------------------------------------
@dataclass
class Relayer:
    publisher: Publisher
    batch_size: int = 200
    max_attempts: int = 5

    def drain_once(self) -> int:
        """Publish at most ``batch_size`` unprocessed rows; return the count.

        Returns 0 when the table is fully drained — caller can sleep.
        """
        published = 0
        with session_scope() as s:
            rows = list(
                s.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.processed_at.is_(None))
                    .where(OutboxEvent.delivery_attempts < self.max_attempts)
                    .order_by(OutboxEvent.created_at.asc())
                    .limit(self.batch_size)
                ).scalars()
            )
            if not rows:
                return 0
            for row in rows:
                record = OutboxRecord(
                    id=row.id,
                    aggregate_type=row.aggregate_type,
                    aggregate_id=row.aggregate_id,
                    event_type=row.event_type,
                    sequence=row.sequence,
                    payload=row.payload,
                )
                try:
                    self.publisher.publish(record)
                except Exception:  # noqa: BLE001
                    row.delivery_attempts += 1
                    log.exception("Outbox publish failed (id=%s, attempts=%d)",
                                  row.id, row.delivery_attempts)
                    continue
                row.processed_at = datetime.now(timezone.utc)
                published += 1
            s.commit()
        return published

    def run_forever(self, idle_sleep_s: float = 1.0) -> None:  # pragma: no cover
        """Long-running drain loop — invoked by the relayer worker process."""
        while True:
            try:
                count = self.drain_once()
                if count == 0:
                    time.sleep(idle_sleep_s)
            except Exception:  # noqa: BLE001
                log.exception("Outbox relayer iteration failed; sleeping before retry")
                time.sleep(idle_sleep_s * 5)


# ---------------------------------------------------------------------------
# Maintenance: prune long-processed rows so the table stays small
# ---------------------------------------------------------------------------
def prune_processed(older_than_seconds: int = 7 * 24 * 3600) -> int:
    """Delete rows the relayer marked processed more than N seconds ago."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    with session_scope() as s:
        deleted = s.execute(
            delete(OutboxEvent)
            .where(OutboxEvent.processed_at.is_not(None))
            .where(OutboxEvent.processed_at < cutoff)
        ).rowcount
        s.commit()
    log.info("Outbox pruned: %d rows older than %ds", deleted or 0, older_than_seconds)
    return deleted or 0
