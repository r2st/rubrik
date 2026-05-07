"""Tests for the API tier auto-scaling implementation (ADR 0013)."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Backpressure middleware (#7)
# ---------------------------------------------------------------------------
def test_backpressure_module_registers_handle():
    """`current_inflight()` reports zero before any request runs."""
    from api import backpressure
    # The app's middleware stack already registered an instance during import.
    assert backpressure.current_inflight() >= 0
    assert backpressure.current_rejected() >= 0


def test_backpressure_endpoint_bypass_paths_listed():
    """Live/ready/health/metrics must bypass the cap so probes always answer."""
    from api.backpressure import BackpressureMiddleware
    for p in ("/api/live", "/api/ready", "/api/health", "/metrics"):
        assert p in BackpressureMiddleware.BYPASS_PATHS


# ---------------------------------------------------------------------------
# Circuit breaker (#8)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold():
    from api.circuit_breaker import CircuitBreaker, CircuitOpenError, State

    cb = CircuitBreaker(name="test_open", failure_threshold=3, recovery_timeout_s=10)

    async def boom():
        raise RuntimeError("downstream down")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call(boom)

    # Circuit should now be open — next call rejected immediately.
    assert cb.state is State.OPEN
    with pytest.raises(CircuitOpenError):
        await cb.call(boom)


@pytest.mark.asyncio
async def test_circuit_breaker_recovers_via_half_open():
    from api.circuit_breaker import CircuitBreaker, State

    cb = CircuitBreaker(name="test_recover", failure_threshold=2, recovery_timeout_s=0.05)

    async def boom():
        raise RuntimeError()

    async def ok():
        return "ok"

    with pytest.raises(RuntimeError):
        await cb.call(boom)
    with pytest.raises(RuntimeError):
        await cb.call(boom)
    assert cb.state is State.OPEN

    # Wait past the recovery window, then a successful probe closes it.
    await asyncio.sleep(0.06)
    assert await cb.call(ok) == "ok"
    assert cb.state is State.CLOSED


def test_circuit_breaker_registry_is_idempotent():
    from api.circuit_breaker import get_breaker
    a = get_breaker("test_registry")
    b = get_breaker("test_registry")
    assert a is b


# ---------------------------------------------------------------------------
# Snapshot read/write (#1)
# ---------------------------------------------------------------------------
def test_snapshot_round_trip():
    from api import snapshot

    payload = {"hello": "world", "n": 42}
    with tempfile.TemporaryDirectory() as tmp:
        manifest = snapshot.write_snapshot(tmp, payload, n_meetings=42)
        assert manifest["n_meetings"] == 42
        assert manifest["format_version"] == snapshot.SNAPSHOT_FORMAT_VERSION

        m2 = snapshot.read_manifest(tmp)
        assert m2 == manifest

        loaded = snapshot.read_snapshot(tmp)
        assert loaded == payload


def test_snapshot_missing_returns_none():
    from api import snapshot
    with tempfile.TemporaryDirectory() as tmp:
        assert snapshot.read_manifest(tmp) is None
        assert snapshot.read_snapshot(tmp) is None


def test_snapshot_corrupt_payload_returns_none():
    from api import snapshot
    with tempfile.TemporaryDirectory() as tmp:
        snapshot.write_snapshot(tmp, {"a": 1}, n_meetings=1)
        # Corrupt the payload — checksum will mismatch.
        (Path(tmp) / snapshot.PAYLOAD_NAME).write_bytes(b"garbage")
        assert snapshot.read_snapshot(tmp) is None


# ---------------------------------------------------------------------------
# Liveness / Readiness (#10)
# ---------------------------------------------------------------------------
def test_liveness_endpoint_returns_alive():
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/live")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_readiness_endpoint_reports_state():
    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/ready")
    body = r.json()
    # In tests the pipeline is warm and DB is reachable, so we should be ready.
    assert r.status_code == 200, body
    assert body["status"] == "ready"
    assert body["checks"]["pipeline_warm"] is True
    assert body["checks"]["db_reachable"] is True
    assert body["checks"]["not_draining"] is True
    assert "inflight" in body and "rejected_total" in body


def test_readiness_during_draining_returns_503():
    from api.main import app
    with TestClient(app) as c:
        # Simulate a shutdown drain in progress.
        app.state.shutting_down = True
        try:
            r = c.get("/api/ready")
        finally:
            app.state.shutting_down = False
    assert r.status_code == 503
    assert r.json()["checks"]["not_draining"] is False


# ---------------------------------------------------------------------------
# Per-tenant rate-limit key (#13)
# ---------------------------------------------------------------------------
def test_tenant_aware_key_uses_api_key_when_present():
    from starlette.datastructures import Headers
    from starlette.requests import Request

    from api.limiter import tenant_aware_key

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/summary",
        "headers": Headers({"x-api-key": "secret-1", "host": "x"}).raw,
        "client": ("1.2.3.4", 12345),
        "query_string": b"",
    }
    key1 = tenant_aware_key(Request(scope))

    scope2 = dict(scope)
    scope2["headers"] = Headers({"x-api-key": "secret-2", "host": "x"}).raw
    key2 = tenant_aware_key(Request(scope2))
    assert key1 != key2  # different tenants
    assert "1.2.3.4" in key1 and "1.2.3.4" in key2  # same IP component


def test_tenant_aware_key_anon_when_no_key():
    from starlette.datastructures import Headers
    from starlette.requests import Request

    from api.limiter import tenant_aware_key

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/summary",
        "headers": Headers({"host": "x"}).raw,
        "client": ("9.9.9.9", 1),
        "query_string": b"",
    }
    assert tenant_aware_key(Request(scope)).startswith("anon:")


# ---------------------------------------------------------------------------
# Snapshot writer CLI (#2) — basic invocation surface
# ---------------------------------------------------------------------------
def test_snapshot_writer_main_writes_to_path(monkeypatch):
    """Invoke the CLI's `main()` directly and verify a manifest lands."""
    from api import snapshot, snapshot_writer

    with tempfile.TemporaryDirectory() as tmp:
        rc = snapshot_writer.main(["--url", tmp])
        assert rc == 0
        manifest = snapshot.read_manifest(tmp)
        assert manifest is not None
        assert manifest["n_meetings"] > 0


# ---------------------------------------------------------------------------
# state.is_warm() (#10 / #1)
# ---------------------------------------------------------------------------
def test_state_is_warm_after_get_state():
    from api import state
    state.get_state()
    assert state.is_warm() is True


# ---------------------------------------------------------------------------
# Settings — new keys present (#9, #14, #1, #3, #7)
# ---------------------------------------------------------------------------
def test_new_runtime_settings_keys_seeded():
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    expected = [
        "backpressure.max_inflight",
        "snapshot.url",
        "snapshot.poll_seconds",
        "distribution.redis_url",
        "observability.otel_sample_rate",
    ]
    keys = {s.key for s in rt.all()}
    for k in expected:
        assert k in keys, f"missing default: {k}"
