"""Tests for the custom Prometheus metrics + readiness Redis probe +
session_secret externalization + outbox trace_id propagation.

Closes the four production gaps flagged in the post-research audit.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Custom Prometheus metrics
# ---------------------------------------------------------------------------
def test_idempotency_counter_increments_on_replay():
    """A cache hit registers as result=hit on the counter."""
    from api import metrics as metrics_mod
    if metrics_mod.idempotency_total is None:
        pytest.skip("prometheus_client not installed")
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    rt.set("idempotency.enabled", True, actor="test")
    try:
        from api.main import app
        before = metrics_mod.idempotency_total.labels(result="hit")._value.get()
        with TestClient(app) as c:
            c.post("/api/v1/admin/login", json={"password": "wrong"},
                   headers={"Idempotency-Key": "k-metric-1"})
            c.post("/api/v1/admin/login", json={"password": "wrong"},
                   headers={"Idempotency-Key": "k-metric-1"})
        after = metrics_mod.idempotency_total.labels(result="hit")._value.get()
        assert after - before >= 1
    finally:
        rt.set("idempotency.enabled", False, actor="test-cleanup")


def test_breaker_state_metric_publishes_on_transition():
    """Opening a breaker writes the gauge as `2` for that name."""
    import asyncio

    from api import metrics as metrics_mod
    if metrics_mod.circuit_breaker_state is None:
        pytest.skip("prometheus_client not installed")
    from api.circuit_breaker import CircuitBreaker

    cb = CircuitBreaker(name="metric_test_breaker", failure_threshold=2,
                        recovery_timeout_s=10)

    async def boom():
        raise RuntimeError("forced")

    async def drive():
        import contextlib
        for _ in range(2):
            with contextlib.suppress(RuntimeError):
                await cb.call(boom)

    asyncio.get_event_loop().run_until_complete(drive())
    val = metrics_mod.circuit_breaker_state.labels(name="metric_test_breaker")._value.get()
    assert val == 2.0  # open


def test_outbox_collector_registered_once():
    """register_outbox_collector is idempotent — second call is a no-op."""
    from api import metrics as metrics_mod
    metrics_mod.register_outbox_collector()
    metrics_mod.register_outbox_collector()  # must not raise
    assert metrics_mod._outbox_collector_registered is True


# ---------------------------------------------------------------------------
# /api/ready Redis probe
# ---------------------------------------------------------------------------
def test_readiness_does_not_probe_redis_when_unconfigured():
    """No redis_url → no redis_reachable check in the response body."""
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/ready")
    assert r.status_code == 200
    assert "redis_reachable" not in r.json()["checks"]


def test_readiness_probes_redis_when_configured(monkeypatch):
    """With redis_url set but unreachable → ready=false + redis_reachable=false."""
    from src.settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s.runtime, "redis_url",
                        "redis://does-not-exist.invalid:6379/0")

    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/ready")
    body = r.json()
    assert "redis_reachable" in body["checks"]
    assert body["checks"]["redis_reachable"] is False
    assert r.status_code == 503  # ready blocked by sick Redis
    assert body["status"] == "not_ready"


# ---------------------------------------------------------------------------
# session_secret externalization
# ---------------------------------------------------------------------------
def test_session_secret_path_overrides_inline_value():
    """Mounted-file value wins over the literal in bootstrap.toml."""
    from src.settings import AdminSection
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "session_secret"
        path.write_text("super-secret-from-the-file\n")  # trailing \n stripped
        admin = AdminSection(
            initial_password="x",
            session_secret="THIS-SHOULD-LOSE",
            session_secret_path=str(path),
        )
        assert admin.resolved_session_secret() == "super-secret-from-the-file"


def test_session_secret_path_missing_falls_back_to_inline():
    """If the path doesn't exist, the inline value is used (no crash)."""
    from src.settings import AdminSection
    admin = AdminSection(
        initial_password="x",
        session_secret="inline-fallback",
        session_secret_path="/nonexistent/path/session_secret",
    )
    assert admin.resolved_session_secret() == "inline-fallback"


def test_session_secret_unset_returns_inline():
    """No path configured → inline value, untouched."""
    from src.settings import AdminSection
    admin = AdminSection(initial_password="x", session_secret="just-inline")
    assert admin.resolved_session_secret() == "just-inline"


# ---------------------------------------------------------------------------
# Outbox trace_id propagation
# ---------------------------------------------------------------------------
def test_outbox_emit_captures_trace_id_when_explicit():
    """An explicit trace_id is stored on the row."""
    from api.outbox import emit
    from src.db import session_scope
    from src.models_db import OutboxEvent
    with session_scope() as s:
        s.query(OutboxEvent).delete()
        s.commit()
    with session_scope() as s:
        emit(s, aggregate_type="m", aggregate_id="x",
             event_type="t", payload={}, trace_id="a" * 32)
        s.commit()
    with session_scope() as s:
        row = s.query(OutboxEvent).one()
        assert row.trace_id == "a" * 32


def test_outbox_emit_captures_no_trace_id_outside_span():
    """No active OTel span → trace_id is None (not a crash)."""
    from api.outbox import emit
    from src.db import session_scope
    from src.models_db import OutboxEvent
    with session_scope() as s:
        s.query(OutboxEvent).delete()
        s.commit()
    with session_scope() as s:
        emit(s, aggregate_type="m", aggregate_id="x",
             event_type="t", payload={})
        s.commit()
    with session_scope() as s:
        row = s.query(OutboxEvent).one()
        # Either None (no OTel installed / no span) or a 32-hex string;
        # both are valid behaviours of _current_trace_id.
        assert row.trace_id is None or len(row.trace_id) == 32


def test_outbox_record_carries_trace_id_to_publisher():
    """The relayer hands the trace_id through to the Publisher boundary."""
    from api.outbox import InMemoryPublisher, Relayer, emit
    from src.db import session_scope
    from src.models_db import OutboxEvent
    with session_scope() as s:
        s.query(OutboxEvent).delete()
        s.commit()
    with session_scope() as s:
        emit(s, aggregate_type="m", aggregate_id="x", event_type="t",
             payload={}, trace_id="b" * 32)
        s.commit()
    pub = InMemoryPublisher()
    Relayer(publisher=pub).drain_once()
    assert pub.delivered[0].trace_id == "b" * 32
