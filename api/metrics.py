"""Custom Prometheus metrics for the production subsystems.

Closes a documentation/implementation gap: ``deploy/k8s/prometheus-slo-rules.yaml``
defines alerts (``StreamLagSLOBreach``, etc.) that reference metrics the
application code didn't actually emit.

What this module declares
-------------------------
- ``transcript_intel_outbox_unprocessed``  (gauge) — current unprocessed-row
  count. Production runs this with KEDA / SLO alerts on lag.
- ``transcript_intel_outbox_stuck``         (gauge) — rows at the
  ``delivery_attempts`` cap. Healthy = 0.
- ``transcript_intel_idempotency_total``    (counter, labelled by
  ``result={hit, miss, conflict, skipped}``) — answers "is the cache
  earning its keep?" and "do we have a client emitting duplicate keys?"
- ``transcript_intel_adaptive_throttle_shed_total``  (counter) — how often
  the third-tier shed fires. Goes up before SLO budget burns.
- ``transcript_intel_circuit_breaker_state``  (gauge, labelled by ``name``)
  — 0 closed · 1 half_open · 2 open. Cardinality is bounded by the breaker
  registry size (handful, not unbounded).

Why a gauge (collected per scrape) for the outbox counts vs a counter:
the outbox row count is a current state, not a monotonic event count.
A small custom collector queries the DB once per scrape and reports the
gauge. We DO NOT increment it in app code — that would be racy and miss
rows mutated by other replicas.

Wiring
------
``api/main.py`` and ``api/admin_app.py`` import this module at startup so
the registered counters/gauges are picked up by the
``prometheus-fastapi-instrumentator`` exporter at ``/metrics`` (already
mounted via ``api/observability.py::install_metrics``).

Read the registry order matters: this module must be imported before
``install_metrics`` runs the ``Instrumentator().expose(app, "/metrics")``
call so the registered series appear in the first scrape.
"""
from __future__ import annotations

from typing import Optional

try:
    from prometheus_client import REGISTRY, Counter, Gauge
    from prometheus_client.core import GaugeMetricFamily
except ImportError:  # pragma: no cover — prometheus_client is a runtime dep
    REGISTRY = None  # type: ignore[assignment]
    Counter = None   # type: ignore[assignment]
    Gauge = None     # type: ignore[assignment]
    GaugeMetricFamily = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Counter / gauge declarations
# ---------------------------------------------------------------------------
_NS = "transcript_intel"

if Counter is not None:
    idempotency_total = Counter(
        f"{_NS}_idempotency_total",
        "Idempotency-Key cache outcomes by result.",
        ["result"],   # hit | miss | conflict | skipped
    )

    adaptive_throttle_shed_total = Counter(
        f"{_NS}_adaptive_throttle_shed_total",
        "Requests shed by the adaptive throttle before reaching the handler.",
    )

    circuit_breaker_state = Gauge(
        f"{_NS}_circuit_breaker_state",
        "Circuit-breaker state by name (0=closed, 1=half_open, 2=open).",
        ["name"],
    )
else:  # pragma: no cover — only when prometheus_client is missing
    idempotency_total = None
    adaptive_throttle_shed_total = None
    circuit_breaker_state = None


# ---------------------------------------------------------------------------
# Outbox collector — queries the DB at scrape time, not via increments
# ---------------------------------------------------------------------------
class _OutboxCollector:
    """Custom collector — runs the count query at every Prometheus scrape.

    Why not increment a counter from the relayer? The outbox table is shared
    across replicas; only the DB knows the true count. A scrape-time query
    is the correct read of the gauge.
    """

    def collect(self):
        if GaugeMetricFamily is None:  # pragma: no cover
            return
        try:
            from sqlalchemy import func, select

            from src.db import session_scope
            from src.models_db import OutboxEvent
        except Exception:  # noqa: BLE001 — DB not initialized
            return

        unprocessed = 0
        stuck = 0
        try:
            with session_scope() as s:
                unprocessed = int(
                    s.execute(
                        select(func.count(OutboxEvent.id))
                        .where(OutboxEvent.processed_at.is_(None))
                    ).scalar() or 0
                )
                stuck = int(
                    s.execute(
                        select(func.count(OutboxEvent.id))
                        .where(OutboxEvent.processed_at.is_(None))
                        .where(OutboxEvent.delivery_attempts >= 5)
                    ).scalar() or 0
                )
        except Exception:  # noqa: BLE001 — DB transient error; report 0/0
            pass

        g_unprocessed = GaugeMetricFamily(
            f"{_NS}_outbox_unprocessed",
            "Unprocessed outbox rows (sum of in-flight + stuck).",
        )
        g_unprocessed.add_metric([], float(unprocessed))
        yield g_unprocessed

        g_stuck = GaugeMetricFamily(
            f"{_NS}_outbox_stuck",
            "Outbox rows that hit delivery_attempts >= max (poison rows).",
        )
        g_stuck.add_metric([], float(stuck))
        yield g_stuck


_outbox_collector_registered = False


def register_outbox_collector() -> None:
    """Register the outbox collector exactly once.

    Safe to call from both the public API and the admin app — the second
    call is a no-op. Idempotent because the prometheus client otherwise
    raises ``ValueError: Duplicated timeseries`` on re-registration.
    """
    global _outbox_collector_registered
    if REGISTRY is None or _outbox_collector_registered:
        return
    REGISTRY.register(_OutboxCollector())
    _outbox_collector_registered = True


# ---------------------------------------------------------------------------
# Convenience setters — the call sites use these instead of touching the
# globals directly. Each is a no-op when prometheus_client isn't installed.
# ---------------------------------------------------------------------------
def record_idempotency(result: str) -> None:
    if idempotency_total is not None:
        idempotency_total.labels(result=result).inc()


def record_throttle_shed() -> None:
    if adaptive_throttle_shed_total is not None:
        adaptive_throttle_shed_total.inc()


def record_breaker_state(name: str, state: str) -> None:
    """Record a breaker state change. State string → numeric for Prometheus."""
    if circuit_breaker_state is None:
        return
    value: Optional[float] = {"closed": 0.0, "half_open": 1.0, "open": 2.0}.get(state)
    if value is not None:
        circuit_breaker_state.labels(name=name).set(value)
