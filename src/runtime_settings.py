"""DB-backed runtime configuration with audit trail.

Anything tunable at runtime — rate limits, churn-risk weights, feature flags,
the auth API key — lives here, not in `bootstrap.toml`. Operators change
values through the admin panel (`/admin`); the next request reads the new
value (within `_TTL_SECONDS`).

Design points:
  - Single-process **read cache** with TTL — avoid hammering SQLite on every
    rate-limited request. TTL is short (5s) so admin changes propagate fast.
  - Every `set()` writes an `AuditLog` row in the same transaction.
  - `bulk_seed()` populates defaults on first start and is safe to re-run.
  - Type info (`str | int | float | bool | list`) is stored alongside the
    value so the admin UI can render the right input control.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sqlalchemy import select

from .db import init_db, session_scope
from .logging_config import get_logger
from .models_db import AuditLog, Setting

log = get_logger(__name__)

_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class SettingDefault:
    """A default value + metadata, used to seed the DB on first start."""
    key: str
    value: Any
    type: str
    category: str
    description: str


# ---------------------------------------------------------------------------
# Default catalogue — every runtime-tunable setting registers here
# ---------------------------------------------------------------------------
DEFAULTS: list[SettingDefault] = [
    # ---- auth ----
    SettingDefault("auth.api_key", "", "str", "auth",
        "If set, every /api/v1/* request requires this in the X-API-Key header. "
        "Empty string = auth disabled (dev mode)."),
    SettingDefault("auth.cors_origins", ["*"], "list", "auth",
        "Allowed CORS origins. Tighten in prod (e.g., ['https://dashboard.example.com'])."),

    # ---- rate limiting ----
    SettingDefault("rate_limit.default", "120/minute", "str", "rate_limit",
        "Default per-IP rate limit (slowapi syntax)."),
    SettingDefault("rate_limit.strict", "30/minute", "str", "rate_limit",
        "Stricter limit for expensive endpoints."),
    SettingDefault("rate_limit.per_tenant", {}, "json", "rate_limit",
        "Per-tenant overrides keyed by hashed API key prefix (16 hex chars). "
        "Example: {\"a3f4...\": \"500/minute\"}. Empty means use the default."),

    # ---- pipeline lifecycle ----
    SettingDefault("pipeline.refresh_minutes", 0, "int", "pipeline",
        "Rebuild the analysis cache every N minutes. 0 = disabled (build at startup only)."),

    # ---- risk scoring ----
    SettingDefault("risk.weight_low_sentiment", 0.5, "float", "risk",
        "Composite churn risk weight for the sentiment-gap signal."),
    SettingDefault("risk.weight_churn_signals", 0.3, "float", "risk",
        "Composite churn risk weight for explicit churn moments."),
    SettingDefault("risk.weight_negative_pivots", 0.2, "float", "risk",
        "Composite churn risk weight for within-meeting sentiment drops."),
    SettingDefault("risk.threshold_high", 0.40, "float", "risk",
        "Risk score above which a customer is tagged 🔴 high tier."),
    SettingDefault("risk.threshold_medium", 0.25, "float", "risk",
        "Risk score above which a customer is tagged 🟡 medium tier."),

    # ---- sentiment / pivots ----
    SettingDefault("sentiment.negative_pivot_threshold", -0.5, "float", "sentiment",
        "max_drop value at or below which a meeting is flagged as a friction event."),

    # ---- feature flags ----
    SettingDefault("feature.metrics_enabled", True, "bool", "feature",
        "Mount the /metrics Prometheus endpoint."),
    SettingDefault("feature.observability_traces", True, "bool", "feature",
        "Forward FastAPI spans through OpenTelemetry (requires otel_endpoint)."),

    # ---- observability ----
    SettingDefault("observability.sentry_traces_sample_rate", 0.1, "float", "observability",
        "Fraction of requests sampled for Sentry tracing (0..1)."),
    SettingDefault("observability.otel_sample_rate", 0.01, "float", "observability",
        "TraceIdRatioBased sample rate for OpenTelemetry traces (0..1). "
        "Errors and slow requests are always sampled regardless via tail-sampling."),

    # ---- backpressure ----
    SettingDefault("backpressure.max_inflight", 128, "int", "backpressure",
        "Per-process inflight-request cap. Exceeding returns 503 + Retry-After. "
        "Size for ~workers × 2 × cpu_count."),

    # ---- snapshot ----
    SettingDefault("snapshot.url", "", "str", "snapshot",
        "Shared PipelineState snapshot location (path or s3://...). "
        "Empty = build locally on warm-up. Set in production to fix HPA cold start."),
    SettingDefault("snapshot.poll_seconds", 30, "int", "snapshot",
        "How often replicas check the snapshot manifest for a new build."),

    # ---- distribution ----
    SettingDefault("distribution.redis_url", "", "str", "distribution",
        "Redis URL for cluster-wide rate limiting + cache pub/sub. "
        "Empty = in-process fallback (single-replica only)."),
]


# ---------------------------------------------------------------------------
# RuntimeSettings — the public read/write API
# ---------------------------------------------------------------------------
class RuntimeSettings:
    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        self._cache_loaded_at: float = 0.0
        self._lock = threading.Lock()

    # ---------------------------- read path ------------------------------
    def _ensure_fresh(self) -> None:
        if time.monotonic() - self._cache_loaded_at < _TTL_SECONDS and self._cache:
            return
        with self._lock:
            if time.monotonic() - self._cache_loaded_at < _TTL_SECONDS and self._cache:
                return
            with session_scope() as s:
                rows = s.execute(select(Setting)).scalars().all()
                self._cache = {r.key: r.value for r in rows}
                self._cache_loaded_at = time.monotonic()

    def get(self, key: str, default: Any = None) -> Any:
        try:
            self._ensure_fresh()
        except Exception:  # noqa: BLE001
            # Pre-init / no DB yet: fall back to in-memory defaults
            return _default_for(key, default)
        return self._cache.get(key, _default_for(key, default))

    def all(self) -> list[Setting]:
        with session_scope() as s:
            return list(s.execute(
                select(Setting).order_by(Setting.category, Setting.key)
            ).scalars().all())

    def by_category(self) -> dict[str, list[Setting]]:
        out: dict[str, list[Setting]] = {}
        for s in self.all():
            out.setdefault(s.category, []).append(s)
        return out

    def audit_log(self, limit: int = 100) -> list[AuditLog]:
        with session_scope() as s:
            return list(s.execute(
                select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
            ).scalars().all())

    # ---------------------------- write path ----------------------------
    def set(self, key: str, value: Any, *, actor: str = "system",
            notes: Optional[str] = None) -> Setting:
        """Update one setting; write an audit log row in the same transaction."""
        spec = _default_spec(key)
        if spec is None:
            raise KeyError(f"Unknown setting: {key}")
        coerced = _coerce(value, spec.type)
        with session_scope() as s:
            existing = s.get(Setting, key)
            old = existing.value if existing else None
            if existing is None:
                existing = Setting(
                    key=key, value=coerced, type=spec.type,
                    category=spec.category, description=spec.description,
                    updated_by=actor,
                )
                s.add(existing)
            else:
                existing.value = coerced
                existing.updated_by = actor
            s.add(AuditLog(
                actor=actor, action="set", setting_key=key,
                old_value=old, new_value=coerced, notes=notes,
            ))
            s.commit()
            s.refresh(existing)
        self._invalidate()
        _publish_change(key)
        log.info("setting changed: %s = %r (actor=%s)", key, coerced, actor)
        return existing

    def reset(self, key: str, *, actor: str = "system") -> Setting:
        spec = _default_spec(key)
        if spec is None:
            raise KeyError(f"Unknown setting: {key}")
        return self.set(key, spec.value, actor=actor, notes="reset to default")

    def bulk_seed(self, defaults: Iterable[SettingDefault] | None = None) -> int:
        """Insert any missing default settings. Idempotent — existing rows untouched."""
        defaults = list(defaults or DEFAULTS)
        inserted = 0
        with session_scope() as s:
            existing_keys = {
                row[0] for row in s.execute(select(Setting.key)).all()
            }
            for d in defaults:
                if d.key in existing_keys:
                    continue
                s.add(Setting(
                    key=d.key, value=d.value, type=d.type,
                    category=d.category, description=d.description,
                    updated_by="seed",
                ))
                inserted += 1
            if inserted:
                s.add(AuditLog(
                    actor="seed", action="bulk_update",
                    notes=f"Seeded {inserted} default(s)",
                ))
                s.commit()
        self._invalidate()
        if inserted:
            log.info("Seeded %d runtime settings", inserted)
        return inserted

    def _invalidate(self) -> None:
        """Drop the cache so the next read reloads from DB.

        Clears `_cache` itself — relying on `_cache_loaded_at = 0.0` is unsafe
        early in the process when `time.monotonic()` returns small values that
        still satisfy the `< TTL` early-return condition.
        """
        with self._lock:
            self._cache = {}
            self._cache_loaded_at = 0.0


# ---------------------------------------------------------------------------
# Push invalidation — Postgres LISTEN/NOTIFY (best effort)
# ---------------------------------------------------------------------------
# When settings change on one replica, every other replica should drop its
# cache immediately rather than waiting for the 5s TTL. Postgres has
# LISTEN/NOTIFY built in; SQLite (dev) doesn't, so this is a no-op there.
#
# We publish via the same connection used to write the row — a separate
# listener thread (started from the FastAPI lifespan) consumes notifications
# and calls _invalidate() locally.
_NOTIFY_CHANNEL = "settings_changed"


def _publish_change(key: str) -> None:
    """Best-effort NOTIFY on Postgres; silent on SQLite or if not connected."""
    try:
        from .db import get_engine  # noqa: PLC0415
        engine = get_engine()
        if "postgres" not in engine.url.drivername:
            return
        # Use a short-lived raw connection so we don't tangle with the ORM session.
        with engine.connect() as conn:
            conn.exec_driver_sql(
                f"NOTIFY {_NOTIFY_CHANNEL}, %s",
                (f"settings:{key}",),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        # Push invalidation is opportunistic — TTL is the safety net.
        log.debug("LISTEN/NOTIFY publish skipped: %s", e)


def handle_notification(payload: str, callback) -> None:
    """Dispatch a single notification payload to ``callback``.

    Extracted from the listener-thread loop so the load-bearing logic — what
    happens *given* a notification — is unit-testable without spinning up
    Postgres. The thread loop is the I/O wrapper; this is the policy.
    """
    try:
        callback(payload)
    except Exception:  # noqa: BLE001
        log.exception("settings listener callback failed (payload=%r)", payload)


def start_listener(callback):
    """Start a background thread that LISTENs for settings_changed notifications.

    Returns a handle with ``.stop()`` (or None if Postgres isn't the backend).
    The callback is invoked as ``callback(payload: str)``.
    """
    try:
        from .db import get_engine  # noqa: PLC0415
        engine = get_engine()
        if "postgres" not in engine.url.drivername:
            log.debug("LISTEN/NOTIFY listener skipped (non-Postgres backend)")
            return None
    except Exception as e:  # noqa: BLE001
        log.debug("LISTEN/NOTIFY listener skipped: %s", e)
        return None

    listener = _ListenerThread(engine, callback)
    listener.start()
    return listener


class _ListenerThread(threading.Thread):
    def __init__(self, engine, callback) -> None:
        super().__init__(daemon=True, name="settings-listener")
        self._engine = engine
        self._callback = callback
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover — requires a real Postgres
        import select as _select
        try:
            raw = self._engine.raw_connection()
            cur = raw.cursor()
            cur.execute(f"LISTEN {_NOTIFY_CHANNEL}")
            while not self._stop.is_set():
                if _select.select([raw], [], [], 1) == ([], [], []):
                    continue
                raw.poll()
                while raw.notifies:
                    n = raw.notifies.pop(0)
                    handle_notification(n.payload or "", self._callback)
        except Exception:  # noqa: BLE001
            log.exception("settings listener crashed; falling back to TTL-only")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_DEFAULTS_BY_KEY = {d.key: d for d in DEFAULTS}


def _default_spec(key: str) -> Optional[SettingDefault]:
    return _DEFAULTS_BY_KEY.get(key)


def _default_for(key: str, fallback: Any) -> Any:
    spec = _default_spec(key)
    return spec.value if spec else fallback


def _coerce(value: Any, type_: str) -> Any:
    """Coerce admin-supplied values (often strings from an HTML form) to the right type."""
    if type_ == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)
    if type_ == "int":
        return int(value)
    if type_ == "float":
        return float(value)
    if type_ == "list":
        if isinstance(value, str):
            # comma-separated → list, JSON-array → list
            v = value.strip()
            if v.startswith("["):
                import json
                return json.loads(v)
            return [s.strip() for s in v.split(",") if s.strip()]
        return list(value)
    if type_ == "json":
        if isinstance(value, str):
            import json
            return json.loads(value) if value.strip() else {}
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Singleton + bootstrap entry point
# ---------------------------------------------------------------------------
_singleton: Optional[RuntimeSettings] = None
_singleton_lock = threading.Lock()


def get_runtime() -> RuntimeSettings:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = RuntimeSettings()
    return _singleton


def initialize_db_and_seed() -> None:
    """Create tables (if missing) and seed default runtime settings."""
    init_db()
    get_runtime().bulk_seed()


def reset_for_tests() -> None:
    """Drop the singleton + invalidate cache. Used in test fixtures."""
    global _singleton
    with _singleton_lock:
        _singleton = None
