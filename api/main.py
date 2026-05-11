"""FastAPI application entry point.

Wires together:
  - Auth (API key via X-API-Key header — disabled when no key set)
  - Standardized error envelope
  - CORS (configurable origins)
  - Security headers + request ID middleware
  - Rate limiting (slowapi)
  - Prometheus metrics, OpenTelemetry tracing, Sentry (all opt-in)
  - Versioned routes at /api/v1/*  (with /api/* deprecated alias)
  - Static frontend at /
  - Periodic pipeline refresh (when configured)

Run dev:  uvicorn api.main:app --reload
Run prod: uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 4
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIASGIMiddleware

from src.db import get_engine
from src.logging_config import configure_logging, get_logger
from src.runtime_settings import (
    get_runtime,
    initialize_db_and_seed,
    start_listener,
)
from src.settings import get_runtime_view, get_settings

from . import adaptive_throttle as adaptive_throttle_mod
from . import backpressure as backpressure_mod
from . import errors as errors_mod
from . import idempotency as idempotency_mod
from . import limiter as limiter_mod
from . import metrics as metrics_mod
from . import observability, state
from .admin.auth import ensure_admin_password_seeded
from .admin.routes import router as admin_router
from .middleware import (
    BodySizeLimitMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
    StateAgeMiddleware,
)
from .routes import public_router, router

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

settings = get_settings()
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app_: FastAPI):
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    app_.state.shutting_down = False  # reset; module-level app is reused under tests

    # Seed the application DB (admin settings + audit log) and admin password.
    # Idempotent — safe on every start.
    initialize_db_and_seed()
    ensure_admin_password_seeded()

    runtime = get_runtime_view()
    log.info("Starting up (env=%s, auth=%s, refresh=%dmin)",
             settings.env,
             "on" if runtime.auth_required else "off",
             runtime.pipeline_refresh_minutes)

    # Settings push invalidation: LISTEN on Postgres for `settings_changed`
    # and drop the local cache so operator changes propagate < 100 ms
    # instead of waiting for the 5 s TTL. No-op on SQLite.
    listener = start_listener(lambda _payload: get_runtime()._invalidate())  # noqa: SLF001

    state.get_state()  # warm pipeline cache (snapshot if available)
    await state.start_refresh_task(runtime.pipeline_refresh_minutes)
    await state.start_snapshot_poll()  # no-op if snapshot.url is unset
    # Outbox reaper loop — idempotent, cheap, prevents outbox_events
    # from growing forever. Configured via the outbox.reap_* settings;
    # the loop itself self-disables when reap_processed_days = 0.
    from api.outbox import start_reaper_task
    await start_reaper_task()

    log.info("Ready to serve")
    yield

    # ----- Graceful shutdown drain -----
    # Order matters: flip readiness, give the LB a beat, then teardown.
    log.info("Shutting down — initiating drain")
    app_.state.shutting_down = True  # /api/ready flips to 503
    await asyncio.sleep(2)

    await state.stop_snapshot_poll()
    await state.stop_refresh_task()
    from api.outbox import stop_reaper_task
    await stop_reaper_task()
    if listener is not None:
        listener.stop()

    try:
        get_engine().dispose()
    except Exception:  # noqa: BLE001
        log.exception("Engine dispose during shutdown failed")
    log.info("Shutdown complete")


# ---------------------------------------------------------------------------
# App + observability
# ---------------------------------------------------------------------------
# DB needs to be ready BEFORE observability/middleware that read runtime settings.
# Lifespan also calls this — initialize_db_and_seed is idempotent.
initialize_db_and_seed()
ensure_admin_password_seeded()

observability.install_sentry(settings)

app = FastAPI(
    title="Transcript Intelligence API",
    version="1.0.0",
    description=(
        "Topic categorization, sentiment analysis, and strategic insights "
        "for B2B meeting transcripts.\n\n"
        f"**Environment:** `{settings.env}`. "
        "Auth is configured at runtime via the admin panel at `/admin`."
    ),
    lifespan=lifespan,
)

errors_mod.register(app)


# ---------------------------------------------------------------------------
# Rate limiting — limits read from the runtime settings store (admin-tunable)
# ---------------------------------------------------------------------------
def _default_rate_limits() -> list[str]:
    return [get_runtime_view().rate_limit_default]


# Cluster-wide global limit (Redis when configured). Per-tenant overrides
# attach to specific routes via `Depends(per_tenant_rate_limit_dep)`.
limiter = limiter_mod.build_limiter(
    redis_url=settings.redis_url,
    default_limits=_default_rate_limits(),
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, errors_mod.http_exception_handler)
app.add_middleware(SlowAPIASGIMiddleware)


# ---------------------------------------------------------------------------
# Cross-cutting middleware (order matters; outer-most listed first)
# ---------------------------------------------------------------------------
# Body-size cap runs first so oversized payloads are rejected before any
# downstream middleware or handler allocates buffers for them.
app.add_middleware(BodySizeLimitMiddleware)
# Backpressure: cap inflight requests; over the cap → 503 + Retry-After.
# Cap is read from runtime settings (admin-tunable).
try:
    _bp_cap = int(get_runtime().get(
        "backpressure.max_inflight", backpressure_mod.DEFAULT_MAX_INFLIGHT
    ))
except Exception:  # noqa: BLE001
    _bp_cap = backpressure_mod.DEFAULT_MAX_INFLIGHT
app.add_middleware(backpressure_mod.BackpressureMiddleware, max_inflight=_bp_cap)
# Adaptive throttle (third rate-limit layer per ADR 0014): probabilistic
# shedding when the rolling p95 latency breaches the SLO. Sheds before the
# inflight cap to avoid retry storms on a degraded downstream.
app.add_middleware(adaptive_throttle_mod.AdaptiveThrottleMiddleware)
# Idempotency-Key cache (opt-in per request via header + per-deploy via the
# `idempotency.enabled` runtime setting). Sits inside the throttles so the
# cached response gets the same load-shedding protections; sits outside
# the auth + business logic so cache lookups bypass downstream work.
app.add_middleware(idempotency_mod.IdempotencyMiddleware)
app.add_middleware(RequestIDMiddleware)


# Tenant identity — pure ASGI middleware (NOT the @app.middleware decorator,
# which routes through starlette's BaseHTTPMiddleware and breaks streaming
# responses). Runs in the event-loop coroutine context so the ContextVar
# set here propagates into the route's threadpool task; the sync route
# then reads ``src.tenant.current_tenant()`` to scope DB queries.
class _TenantContextMiddleware:
    """Pure ASGI middleware setting the tenant ContextVar per request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from src.tenant import derive_tenant_id, set_current_tenant
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers") or []
        }
        api_key = headers.get("x-api-key") or None
        # JWT claims aren't available at the ASGI layer (require_jwt is a
        # route dep that runs later); the API-key hash is a reasonable
        # tenant proxy until the route handler upgrades the ContextVar
        # via the JWT-aware dep when JWT auth is enabled.
        tenant = derive_tenant_id(jwt_claims=None, api_key=api_key)
        set_current_tenant(tenant)
        await self.app(scope, receive, send)


app.add_middleware(_TenantContextMiddleware)

app.add_middleware(
    SecurityHeadersMiddleware,
    hsts=settings.is_prod,  # only assert HSTS when actually behind TLS
)
app.add_middleware(StateAgeMiddleware)

# CORS — refuse the most common foot-gun: ``allow_origins=["*"]`` plus
# ``allow_credentials=True`` is silently downgraded by browsers but
# remains exploitable by some legacy stacks. If the operator left the
# default at "*" in prod, log loudly and strip the wildcard. Empty
# list (the new default) blocks cross-origin entirely — operators must
# set an explicit allowlist to enable the dashboard from another origin.
def _safe_cors_origins() -> list[str]:
    raw = list(get_runtime_view().cors_origins or [])
    if "*" in raw and settings.is_prod:
        import logging as _logging
        _logging.getLogger("api.main").warning(
            "CORS: auth.cors_origins contains '*' in production — stripping "
            "wildcard because allow_credentials=True. Set an explicit "
            "allowlist (e.g., ['https://dashboard.example.com']).",
        )
        raw = [o for o in raw if o != "*"]
    return raw


app.add_middleware(
    CORSMiddleware,
    allow_origins=_safe_cors_origins(),
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*", "X-API-Key", "X-Request-ID"],
    allow_credentials=True,  # admin session cookie needs this
    expose_headers=["X-Request-ID", "ETag", "X-State-Age-Seconds", "X-Stale-Response"],
)

# Compress JSON payloads >500 bytes — typical /api/v1/meetings response goes
# from ~30 KB to ~5 KB.
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)


# ---------------------------------------------------------------------------
# Observability (must come after app construction)
# ---------------------------------------------------------------------------
# Register custom metrics BEFORE the instrumentator's exposer mounts /metrics
# so the first scrape sees them.
metrics_mod.register_outbox_collector()
observability.install_metrics(app, settings)
observability.install_tracing(app, settings)


# ---------------------------------------------------------------------------
# Routers — public (no auth) + v1 (auth-gated) + admin (session-gated)
# ---------------------------------------------------------------------------
app.include_router(public_router)
app.include_router(router)
app.include_router(admin_router)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
if (WEB_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    for name in ("favicon.svg", "favicon.ico"):
        fpath = WEB_DIR / "static" / name
        if fpath.exists():
            return FileResponse(fpath)
    return FileResponse(WEB_DIR / "index.html", status_code=404)


@app.get("/robots.txt", include_in_schema=False)
def robots() -> Response:
    """Block crawlers from the admin surface; the API itself is
    self-describing via OpenAPI, so we allow the public root."""
    body = (
        "User-agent: *\n"
        "Disallow: /admin\n"
        "Disallow: /api/v1/admin\n"
        "Allow: /\n"
    )
    return Response(content=body, media_type="text/plain")


@app.get("/.well-known/security.txt", include_in_schema=False)
def security_txt() -> Response:
    """RFC 9116 — point researchers at the disclosure channel."""
    body = (
        "Contact: mailto:sumaninster7@gmail.com\n"
        "Expires: 2027-01-01T00:00:00Z\n"
        "Preferred-Languages: en\n"
        "Canonical: /.well-known/security.txt\n"
        "Policy: https://example.com/SECURITY.md\n"
    )
    return Response(content=body, media_type="text/plain")
