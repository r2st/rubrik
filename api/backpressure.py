"""Concurrency-cap backpressure middleware.

uvicorn happily queues requests forever. Slow downstream → request pile-up
→ OOM → cascade failure. This middleware caps the number of in-flight
requests per process; over the cap returns `503 Service Unavailable` with a
jittered `Retry-After` so the load balancer can shed load instead of
escalating it.

The cap is read from runtime settings (`backpressure.max_inflight`), so
operators can tune it without a deploy. Sensible default sized for a
single uvicorn worker on a 4-core box (~4 × cpu × 8 = 128).
"""
from __future__ import annotations

import asyncio
import random
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.logging_config import get_logger

log = get_logger(__name__)

DEFAULT_MAX_INFLIGHT = 128


class BackpressureMiddleware(BaseHTTPMiddleware):
    """Per-process inflight-request cap → 503 + Retry-After when exceeded.

    The cap is enforced via a ``BoundedSemaphore`` so the count is exact.
    Health/readiness/metrics endpoints bypass the cap (load balancers must
    always be able to probe).
    """

    BYPASS_PATHS = ("/api/live", "/api/ready", "/api/health", "/metrics")

    def __init__(self, app: ASGIApp, max_inflight: int = DEFAULT_MAX_INFLIGHT) -> None:
        super().__init__(app)
        self.max_inflight = max_inflight
        self._inflight = 0
        self._inflight_lock = asyncio.Lock()
        self._rejected = 0
        # Self-register so /api/ready can read the inflight count.
        register_active(self)

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def rejected_total(self) -> int:
        return self._rejected

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(self.BYPASS_PATHS):
            return await call_next(request)

        async with self._inflight_lock:
            if self._inflight >= self.max_inflight:
                return self._too_busy(request)
            self._inflight += 1
        try:
            return await call_next(request)
        finally:
            async with self._inflight_lock:
                self._inflight -= 1

    def _too_busy(self, request: Request) -> JSONResponse:
        self._rejected += 1
        # Jittered Retry-After: 1-5s. Helps avoid thundering-herd retries.
        retry_after = random.randint(1, 5)  # noqa: S311 — jitter not crypto
        log.warning(
            "Backpressure: rejecting request (cap=%d, rejected_total=%d)",
            self.max_inflight, self._rejected,
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "service_unavailable",
                    "message": "Server is overloaded; please retry.",
                    "request_id": getattr(request.state, "request_id", None),
                    "path": str(request.url.path),
                }
            },
            headers={"Retry-After": str(retry_after)},
        )


# Module-level handle so /api/ready can read the inflight count.
_active: Optional[BackpressureMiddleware] = None


def register_active(mw: BackpressureMiddleware) -> None:
    global _active
    _active = mw


def current_inflight() -> int:
    return _active.inflight if _active is not None else 0


def current_rejected() -> int:
    return _active.rejected_total if _active is not None else 0
