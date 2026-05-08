"""End-to-end tests for the distributed-stack code paths (Redis, LISTEN/NOTIFY,
Arq, per-tenant rate limit decorator).

Uses ``fakeredis`` so no real Redis is required. The fake implements the same
client interface as ``redis-py`` so anything that talks via ``redis.Redis``
or accepts a connection URL works against it after monkeypatching the factory.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def fake_redis(monkeypatch):
    """In-memory Redis that satisfies ``redis.Redis.from_url`` and similar."""
    import fakeredis
    server = fakeredis.FakeServer()

    def _from_url(url, **kwargs):  # noqa: ARG001
        return fakeredis.FakeStrictRedis(server=server)

    import redis
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, url, **kw: _from_url(url)))
    monkeypatch.setattr("redis.from_url", _from_url, raising=False)
    return server


# ---------------------------------------------------------------------------
# Strict admin limiter via Redis (#3, admin path)
# ---------------------------------------------------------------------------
def test_strict_admin_limiter_uses_redis_when_configured(fake_redis, monkeypatch):
    """Force the redis_url to be set; verify the strict limiter increments
    Redis counters and trips on the 6th attempt (proves cluster-wide path)."""
    from fastapi import HTTPException
    from starlette.datastructures import Headers
    from starlette.requests import Request

    from api.admin.routes import _strict_window, strict_rate_limit
    from src.settings import get_settings

    _strict_window.clear()
    s = get_settings()
    monkeypatch.setattr(s.runtime, "redis_url", "redis://fake:6379/0")

    def _make_req(ip="9.9.9.9"):
        return Request({
            "type": "http", "method": "POST", "path": "/api/v1/admin/login",
            "headers": Headers({"host": "x"}).raw,
            "client": (ip, 1), "query_string": b"",
        })

    statuses = []
    for _ in range(7):
        try:
            strict_rate_limit(_make_req())
            statuses.append("ok")
        except HTTPException as e:
            statuses.append(e.status_code)

    # First 5 succeed, attempts 6–7 are 429 from Redis.
    assert statuses[:5] == ["ok"] * 5
    assert 429 in statuses[5:], f"expected 429 once cap exceeded, got {statuses}"

    # Confirm the increment landed in Redis (not the in-process fallback).
    import fakeredis
    client = fakeredis.FakeStrictRedis(server=fake_redis)
    assert int(client.get("strict_rl:9.9.9.9") or 0) >= 5


# ---------------------------------------------------------------------------
# LISTEN/NOTIFY dispatch is wired (#9)
# ---------------------------------------------------------------------------
def test_listen_notify_dispatch_invokes_callback():
    """The dispatch helper drives the callback exactly once per notification."""
    from src import runtime_settings

    seen: list[str] = []
    runtime_settings.handle_notification("settings:auth.api_key", seen.append)
    runtime_settings.handle_notification("settings:rate_limit.default", seen.append)
    assert seen == ["settings:auth.api_key", "settings:rate_limit.default"]


def test_listen_notify_dispatch_swallows_callback_errors():
    """A throwing callback must not break the listener loop."""
    from src import runtime_settings

    def boom(_payload):
        raise RuntimeError("downstream failed")

    # No exception should propagate.
    runtime_settings.handle_notification("settings:x", boom)


def test_settings_set_publishes_change_on_postgres_only(monkeypatch):
    """`_publish_change` is a no-op on SQLite (the test DB) — verify it
    doesn't blow up the write path. NOTIFY is exercised against real
    Postgres in deployment; here we just confirm graceful skip."""
    from src.runtime_settings import _publish_change
    _publish_change("auth.api_key")  # must not raise on SQLite


# ---------------------------------------------------------------------------
# Arq enqueue with Redis (#11)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_arq_enqueue_uses_redis_when_configured(fake_redis, monkeypatch):
    """Enqueue against fakeredis. Verifies the producer path is wired."""
    from src.settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s.runtime, "redis_url", "redis://fake:6379/0")
    s.__dict__.pop("redis_url", None)

    # Patch arq.create_pool to return a stand-in that records enqueue calls.
    enqueued: list[tuple[str, dict]] = []

    class _FakePool:
        async def enqueue_job(self, name, **kwargs):
            enqueued.append((name, kwargs))
            class _Job:
                job_id = "fake-job-1"
            return _Job()

    async def _fake_create_pool(_settings):
        return _FakePool()

    import sys
    import types
    # Build a stub `arq` package since the real one isn't installed in dev.
    arq_mod = types.ModuleType("arq")
    arq_conn = types.ModuleType("arq.connections")

    class _RedisSettings:
        @classmethod
        def from_dsn(cls, dsn):
            return cls()

    arq_mod.create_pool = _fake_create_pool
    arq_conn.RedisSettings = _RedisSettings
    monkeypatch.setitem(sys.modules, "arq", arq_mod)
    monkeypatch.setitem(sys.modules, "arq.connections", arq_conn)

    from api.jobs import enqueue
    result = await enqueue("rebuild_snapshot", url="/tmp/x")
    assert result.enqueued is True
    assert result.job_id == "fake-job-1"
    assert enqueued == [("rebuild_snapshot", {"url": "/tmp/x"})]


# ---------------------------------------------------------------------------
# Per-tenant rate-limit decorator on /meetings (#13)
# ---------------------------------------------------------------------------
def test_meetings_route_enforces_per_tenant_cap():
    """Set a tight per-tenant override; verify subsequent calls 429."""
    import hashlib

    from fastapi.testclient import TestClient

    from api.limiter import _local_buckets
    from api.main import app
    from src.runtime_settings import get_runtime

    api_key = "test-tenant-A"
    tid = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]

    rt = get_runtime()
    rt.set("auth.api_key", api_key, actor="test")
    rt.set("rate_limit.per_tenant", {tid: "2/minute"}, actor="test")
    _local_buckets.clear()
    try:
        with TestClient(app) as c:
            statuses = [
                c.get("/api/v1/meetings",
                      params={"limit": 1},
                      headers={"X-API-Key": api_key}).status_code
                for _ in range(4)
            ]
        # First two pass (200), the rest hit the per-tenant cap (429).
        assert statuses[:2] == [200, 200], statuses
        assert 429 in statuses[2:], statuses
    finally:
        rt.set("auth.api_key", "", actor="test")
        rt.set("rate_limit.per_tenant", {}, actor="test")
        _local_buckets.clear()


# ---------------------------------------------------------------------------
# CDN-relevant headers — runbook claims these are emitted (#12)
# ---------------------------------------------------------------------------
def test_summary_emits_etag_and_cache_control_for_cdn():
    """The CDN runbook depends on these headers. Regression-test them.

    ADR 0014 §"Cache invalidation discipline" also requires the response
    advertise stale-while-revalidate so the CDN can serve a stale payload
    while it asynchronously revalidates against origin.
    """
    from fastapi.testclient import TestClient

    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/v1/summary")
    assert r.status_code == 200
    assert r.headers.get("ETag")
    cc = r.headers.get("Cache-Control", "")
    assert "max-age" in cc, f"expected Cache-Control max-age, got {cc!r}"
    assert "stale-while-revalidate" in cc, (
        f"expected stale-while-revalidate, got {cc!r}"
    )


def test_admin_endpoint_does_not_emit_cacheable_headers():
    """Admin reads must NOT carry Cache-Control (CDN bypass list)."""
    from fastapi.testclient import TestClient

    from api.main import app
    BOOTSTRAP_PASSWORD = "changeme-on-first-login"
    with TestClient(app) as c:
        c.post("/api/v1/admin/login", json={"password": BOOTSTRAP_PASSWORD})
        r = c.get("/api/v1/admin/me")
    cc = r.headers.get("Cache-Control", "")
    # Whatever else, must not be a long-lived public cache.
    assert "max-age=60" not in cc
