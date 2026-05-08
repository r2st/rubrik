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


# ---------------------------------------------------------------------------
# DLQ + operator surface
# ---------------------------------------------------------------------------
def test_dlq_publisher_routes_to_secondary():
    """`DLQPublisher.publish_dlq()` calls the dlq publisher, not the primary."""
    from api.outbox import DLQPublisher, InMemoryPublisher

    primary = InMemoryPublisher()
    dlq = InMemoryPublisher()
    pub = DLQPublisher(primary, dlq)

    record = type("R", (), {
        "id": 1, "aggregate_type": "m", "aggregate_id": "x",
        "event_type": "t", "sequence": 0, "payload": {},
    })()
    pub.publish(record)
    pub.publish_dlq(record)
    assert primary.delivered == [record]
    assert dlq.delivered == [record]


def test_relayer_routes_poison_rows_to_dlq():
    """delivery_attempts >= max_attempts → DLQ + marked processed."""
    from api.outbox import DLQPublisher, InMemoryPublisher

    primary = InMemoryPublisher()
    dlq = InMemoryPublisher()
    relayer = Relayer(publisher=DLQPublisher(primary, dlq), max_attempts=3)

    with session_scope() as s:
        emit(s, aggregate_type="m", aggregate_id="poison",
             event_type="t", payload={})
        s.commit()
        s.query(OutboxEvent).update({"delivery_attempts": 5})
        s.commit()
    relayer.drain_once()

    # Routed to DLQ, NOT primary; row marked processed.
    assert primary.delivered == []
    assert len(dlq.delivered) == 1
    assert dlq.delivered[0].aggregate_id == "poison"
    with session_scope() as s:
        unprocessed = s.query(OutboxEvent).filter(
            OutboxEvent.processed_at.is_(None)
        ).count()
    assert unprocessed == 0


def test_count_stuck_rows_returns_zero_when_healthy():
    from api.outbox import count_stuck_rows
    assert count_stuck_rows(max_attempts=5) == 0


def test_count_stuck_rows_counts_rows_at_attempt_cap():
    from api.outbox import count_stuck_rows
    with session_scope() as s:
        for _ in range(3):
            emit(s, aggregate_type="m", aggregate_id="x",
                 event_type="t", payload={})
        s.commit()
        s.query(OutboxEvent).update({"delivery_attempts": 7})
        s.commit()
    assert count_stuck_rows(max_attempts=5) == 3


def test_replay_stuck_rows_resets_attempts():
    """`replay_stuck_rows` zeroes delivery_attempts so the next drain retries."""
    from api.outbox import replay_stuck_rows
    with session_scope() as s:
        for _ in range(2):
            emit(s, aggregate_type="m", aggregate_id="x",
                 event_type="t", payload={})
        s.commit()
        s.query(OutboxEvent).update({"delivery_attempts": 6})
        s.commit()

    reset = replay_stuck_rows(max_attempts=5)
    assert reset == 2
    with session_scope() as s:
        for row in s.query(OutboxEvent).all():
            assert row.delivery_attempts == 0


def test_admin_outbox_endpoints():
    """Admin can see stuck-row count and trigger a replay."""
    from fastapi.testclient import TestClient

    from api.main import app
    BOOTSTRAP = "changeme-on-first-login"

    # Seed a stuck row.
    with session_scope() as s:
        emit(s, aggregate_type="m", aggregate_id="x",
             event_type="t", payload={})
        s.commit()
        s.query(OutboxEvent).update({"delivery_attempts": 7})
        s.commit()

    with TestClient(app) as c:
        c.post("/api/v1/admin/login", json={"password": BOOTSTRAP})

        r = c.get("/api/v1/admin/outbox/stuck")
        assert r.status_code == 200
        assert r.json()["stuck"] == 1

        r = c.post("/api/v1/admin/outbox/replay")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "reset": 1, "actor": "admin"}

        # After replay, the same endpoint reports zero stuck rows.
        r = c.get("/api/v1/admin/outbox/stuck")
        assert r.json()["stuck"] == 0
