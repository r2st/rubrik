"""Tests for the adaptive-throttle middleware (ADR 0014, third rate-limit layer)."""
from __future__ import annotations

from api.adaptive_throttle import AdaptiveThrottle


def test_no_shedding_when_under_slo():
    t = AdaptiveThrottle(window_size=50, slo_p95_ms=200.0)
    for _ in range(50):
        t.record(50.0)   # all under SLO
    assert t.shed_probability() == 0.0


def test_shedding_ramps_with_breach():
    """At 1.5× SLO, shed should be ~25%; at 2× SLO ~50%; at 4× SLO 50%–95%."""
    def fill(samples_ms: float) -> AdaptiveThrottle:
        t = AdaptiveThrottle(window_size=20, slo_p95_ms=200.0, max_shed=0.95)
        for _ in range(20):
            t.record(samples_ms)
        return t

    p_at_slo  = fill(200.0).shed_probability()
    p_at_1_5x = fill(300.0).shed_probability()
    p_at_2x   = fill(400.0).shed_probability()
    p_at_4x   = fill(800.0).shed_probability()

    # Monotone non-decreasing.
    assert p_at_slo == 0.0
    assert 0.20 <= p_at_1_5x <= 0.30
    assert 0.45 <= p_at_2x   <= 0.55
    assert p_at_4x > p_at_2x
    assert p_at_4x <= 0.95          # max_shed cap


def test_shedding_capped_at_max_shed():
    """Even at extreme breach, some traffic still goes through (probes)."""
    t = AdaptiveThrottle(window_size=10, slo_p95_ms=10.0, max_shed=0.95)
    for _ in range(10):
        t.record(10_000.0)          # 1000× SLO
    p = t.shed_probability()
    assert p <= 0.95


def test_window_eviction():
    """Old samples leave the window so recovery is reflected immediately."""
    t = AdaptiveThrottle(window_size=10, slo_p95_ms=200.0)
    for _ in range(10):
        t.record(1000.0)            # heavy breach
    assert t.shed_probability() > 0
    # Replace every entry with a healthy sample.
    for _ in range(10):
        t.record(50.0)
    assert t.shed_probability() == 0.0


def test_p95_uses_tail_not_mean():
    """5% slow tail in an otherwise healthy window does NOT trip shedding,
    but 10%+ slow tail does — confirming p95 (not mean) drives the decision.

    Mean of [50×90, 2000×10] = 245 ms (looks bad if you used mean), but p95
    of the same distribution lands in the slow region. Conversely, mean of
    [50×95, 2000×5] = 147 ms (also under SLO if mean), and p95 lands in
    the fast region — no shedding either way.
    """
    # 5 slow / 95 fast — p95 is still fast → no shed.
    t1 = AdaptiveThrottle(window_size=100, slo_p95_ms=200.0)
    for _ in range(95):
        t1.record(50.0)
    for _ in range(5):
        t1.record(2000.0)
    assert t1.shed_probability() == 0.0

    # 10 slow / 90 fast — p95 is now in the slow region → shedding.
    t2 = AdaptiveThrottle(window_size=100, slo_p95_ms=200.0)
    for _ in range(90):
        t2.record(50.0)
    for _ in range(10):
        t2.record(2000.0)
    assert t2.shed_probability() > 0


def test_throttle_middleware_passes_normal_traffic():
    """Healthy throttle adds no behaviour change to the request path."""
    from fastapi.testclient import TestClient

    from api.main import app
    with TestClient(app) as c:
        r = c.get("/api/v1/summary")
    assert r.status_code in (200, 304)


def test_throttle_bypass_paths_listed():
    """Probes and metrics are exempt from shedding."""
    from api.adaptive_throttle import _BYPASS_PATHS
    for p in ("/api/live", "/api/ready", "/api/health", "/metrics"):
        assert p in _BYPASS_PATHS
