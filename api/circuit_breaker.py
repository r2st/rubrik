"""Tiny async circuit breaker.

Wraps an external call (DB, vLLM, frontier-LLM gateway) so that when it
starts failing we **stop hammering it**, return immediately to the caller,
and probe occasionally to detect recovery. Without this, a slow downstream
turns into a request pile-up and an OOM.

State machine
-------------

    closed   ── failure_threshold reached ──▶ open
    open     ── recovery_timeout elapsed  ──▶ half_open
    half_open── success                   ──▶ closed
    half_open── failure                   ──▶ open

We deliberately keep the implementation < 100 LOC instead of pulling in
``pybreaker`` / ``purgatory`` — the failure modes here are simple enough,
and the test surface stays trivial. Swap if requirements grow.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, TypeVar

from src.logging_config import get_logger

log = get_logger(__name__)

T = TypeVar("T")


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    recovery_timeout_s: float = 30.0
    # internal
    _state: State = State.CLOSED
    _failures: int = 0
    _opened_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def state(self) -> State:
        return self._state

    async def call(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Execute ``fn`` under the breaker. Raises ``CircuitOpenError`` if open."""
        transitioned = False
        async with self._lock:
            if self._state is State.OPEN:
                if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                    self._state = State.HALF_OPEN
                    transitioned = True
                    log.info("Circuit %s -> half_open (probing)", self.name)
                else:
                    raise CircuitOpenError(f"Circuit {self.name!r} is open")
        if transitioned:
            self._publish_state()

        try:
            result = await fn(*args, **kwargs)
        except Exception:
            await self._on_failure()
            raise
        await self._on_success()
        return result

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state is State.HALF_OPEN:
                log.info("Circuit %s -> closed (recovered)", self.name)
            self._state = State.CLOSED
            self._failures = 0
        self._publish_state()

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state is State.HALF_OPEN or self._failures >= self.failure_threshold:
                if self._state is not State.OPEN:
                    log.warning(
                        "Circuit %s -> open (failures=%d, threshold=%d)",
                        self.name, self._failures, self.failure_threshold,
                    )
                self._state = State.OPEN
                self._opened_at = time.monotonic()
        self._publish_state()

    def _publish_state(self) -> None:
        """Best-effort Prometheus gauge update; silent if metrics not wired."""
        try:
            from . import metrics
            metrics.record_breaker_state(self.name, self._state.value)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Registry — module-level singletons keyed by name
# ---------------------------------------------------------------------------
_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs: Any) -> CircuitBreaker:
    """Get-or-create a named breaker (idempotent)."""
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name=name, **kwargs)
    return _breakers[name]


def all_breakers() -> dict[str, CircuitBreaker]:
    """Snapshot of every breaker for readiness / metrics surfaces."""
    return dict(_breakers)
