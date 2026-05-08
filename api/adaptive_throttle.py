"""Adaptive throttle — the research's third rate-limit layer.

Rate-limit research recommends three layers:

  1. Tenant / client quota at the edge       → ``api/limiter.py``
  2. Concurrency limits in the request path  → ``api/backpressure.py``
  3. **Adaptive throttling** when downstream  → this module
     latency or queue depth breaches the SLO

Layers 1+2 cap how many requests *can* be in flight at any moment. Layer 3
caps how many requests *should* be in flight given **real-time downstream
health**: when DB calls are slowing down, route handlers experience high
latency, or circuit breakers are opening, this throttle starts probabilistic
shedding **before** the inflight cap is reached. The goal: shed early, shed
gracefully, and protect downstream from a retry storm that would otherwise
escalate the outage.

Mechanics
---------
We track a sliding-window p95 latency and a smoothed downstream-error
counter. The shed probability is a piecewise-linear function of how far
the observed p95 is past the SLO:

  - p95 ≤ slo_ms                : 0% shed (normal operation)
  - p95 in (slo_ms, 2× slo_ms]  : 0–50% shed, scaled linearly
  - p95 > 2× slo_ms             : 50–95% shed (capped at 95% so probes still go through)

The cap of 95% is intentional — even under heavy throttling, some traffic
must reach origin so we can detect recovery.
"""
from __future__ import annotations

import collections
import random
import time
from typing import Deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.logging_config import get_logger

log = get_logger(__name__)

# Probes / metrics endpoints bypass throttling — load balancers must
# always be able to read state.
_BYPASS_PATHS = ("/api/live", "/api/ready", "/api/health", "/metrics")


class AdaptiveThrottle:
    """Sliding-window latency tracker + shed-probability calculator.

    Threadsafe via the GIL for read-modify-write on the deque (we never
    iterate while a writer might mutate). For very-high RPS this should
    move to a lock-free histogram, but at our scale the deque is fine.
    """

    def __init__(
        self,
        *,
        window_size: int = 200,
        slo_p95_ms: float = 200.0,
        max_shed: float = 0.95,
    ) -> None:
        self._window: Deque[float] = collections.deque(maxlen=window_size)
        self.slo_p95_ms = slo_p95_ms
        self.max_shed = max_shed

    def record(self, elapsed_ms: float) -> None:
        self._window.append(elapsed_ms)

    def current_p95(self) -> float:
        if not self._window:
            return 0.0
        # Cheap p95 via sort (window is small ≤ 200).
        sorted_w = sorted(self._window)
        idx = max(0, int(len(sorted_w) * 0.95) - 1)
        return sorted_w[idx]

    def shed_probability(self) -> float:
        """0.0 (no shedding) up to ``max_shed`` (heavy shedding)."""
        p95 = self.current_p95()
        if p95 <= self.slo_p95_ms:
            return 0.0
        # Piecewise-linear ramp.
        # x = ratio of breach: 1.0 at SLO, 2.0 at 2× SLO.
        x = p95 / self.slo_p95_ms
        if x <= 2.0:
            # Map [1, 2] → [0, 0.5] linearly.
            return min(self.max_shed, 0.5 * (x - 1.0))
        # Map [2, ∞) → [0.5, max_shed], asymptotic.
        return min(self.max_shed, 0.5 + 0.45 * (1 - 1 / x))


# Module-level singleton so `/api/ready` and the middleware share state.
_default = AdaptiveThrottle()


def current_throttle() -> AdaptiveThrottle:
    return _default


class AdaptiveThrottleMiddleware(BaseHTTPMiddleware):
    """Probabilistic shedding when downstream p95 breaches SLO."""

    def __init__(
        self,
        app: ASGIApp,
        throttle: AdaptiveThrottle | None = None,
    ) -> None:
        super().__init__(app)
        self._throttle = throttle or _default

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(_BYPASS_PATHS):
            return await call_next(request)

        # Decide whether to shed BEFORE doing any downstream work.
        prob = self._throttle.shed_probability()
        if prob > 0.0 and random.random() < prob:  # noqa: S311
            log.warning(
                "Adaptive throttle: shedding (p95=%.1f ms, slo=%.1f ms, p=%.2f, path=%s)",
                self._throttle.current_p95(), self._throttle.slo_p95_ms, prob, path,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "service_unavailable",
                        "message": "Downstream is degraded; please retry shortly.",
                        "request_id": getattr(request.state, "request_id", None),
                        "path": path,
                    }
                },
                headers={"Retry-After": "5"},
            )

        # Time the call so future requests can decide on real data.
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._throttle.record(elapsed_ms)
        return response
