"""Tests for the transactional outbox + relayer (ADR 0014)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.outbox import InMemoryPublisher, Relayer, emit, prune_processed
from src.db import session_scope
from src.models_db import OutboxEvent


@pytest.fixture(autouse=True)
def _clean_outbox():
    """Each test starts with an empty outbox table."""
    with session_scope() as s:
        s.query(OutboxEvent).delete()
        s.commit()
    yield
    with session_scope() as s:
        s.query(OutboxEvent).delete()
        s.commit()


def test_emit_inserts_row_inside_caller_transaction():
    with session_scope() as s:
        emit(s, aggregate_type="meeting", aggregate_id="m-1",
             event_type="meeting.categorized", payload={"call_type": "support"})
        s.commit()
    with session_scope() as s:
        rows = s.query(OutboxEvent).all()
    assert len(rows) == 1
    assert rows[0].aggregate_type == "meeting"
    assert rows[0].event_type == "meeting.categorized"
    assert rows[0].payload == {"call_type": "support"}
    assert rows[0].processed_at is None


def test_emit_rolls_back_with_caller_transaction():
    """If the surrounding transaction rolls back, the event must too."""
    try:
        with session_scope() as s:
            emit(s, aggregate_type="meeting", aggregate_id="m-2",
                 event_type="meeting.categorized", payload={})
            raise RuntimeError("simulated failure after emit")
    except RuntimeError:
        pass
    with session_scope() as s:
        assert s.query(OutboxEvent).count() == 0


def test_relayer_drains_unprocessed_rows_in_order():
    pub = InMemoryPublisher()
    with session_scope() as s:
        for i in range(5):
            emit(s, aggregate_type="meeting", aggregate_id=f"m-{i}",
                 event_type="x", payload={"i": i}, sequence=i)
        s.commit()

    drained = Relayer(publisher=pub).drain_once()
    assert drained == 5
    # All published in insertion order.
    assert [r.aggregate_id for r in pub.delivered] == [f"m-{i}" for i in range(5)]
    # And marked processed in the DB.
    with session_scope() as s:
        unprocessed = s.query(OutboxEvent).filter(
            OutboxEvent.processed_at.is_(None)
        ).count()
    assert unprocessed == 0


def test_relayer_increments_attempts_on_publisher_failure():
    """A throwing publisher leaves rows unprocessed and increments attempts."""
    class Boom:
        def publish(self, _event):
            raise RuntimeError("kafka unreachable")

    with session_scope() as s:
        emit(s, aggregate_type="m", aggregate_id="x", event_type="t", payload={})
        s.commit()
    Relayer(publisher=Boom()).drain_once()
    with session_scope() as s:
        row = s.query(OutboxEvent).one()
        assert row.processed_at is None
        assert row.delivery_attempts == 1


def test_relayer_skips_rows_at_attempt_cap():
    """Rows whose delivery_attempts >= max_attempts are not retried."""
    pub = InMemoryPublisher()
    with session_scope() as s:
        emit(s, aggregate_type="m", aggregate_id="x", event_type="t", payload={})
        s.commit()
        s.query(OutboxEvent).update({"delivery_attempts": 5})
        s.commit()
    drained = Relayer(publisher=pub, max_attempts=5).drain_once()
    assert drained == 0
    assert pub.delivered == []


def test_drain_returns_zero_when_table_empty():
    pub = InMemoryPublisher()
    assert Relayer(publisher=pub).drain_once() == 0


def test_prune_removes_old_processed_rows():
    """`prune_processed` removes rows whose processed_at is older than cutoff."""
    with session_scope() as s:
        emit(s, aggregate_type="m", aggregate_id="x", event_type="t", payload={})
        s.commit()
        # Mark processed in the past.
        s.query(OutboxEvent).update({
            "processed_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
        })
        s.commit()
    deleted = prune_processed(older_than_seconds=60)
    assert deleted == 1
    with session_scope() as s:
        assert s.query(OutboxEvent).count() == 0
