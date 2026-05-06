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

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIASGIMiddleware
from slowapi.util import get_remote_address

from src.logging_config import configure_logging, get_logger
from src.settings import get_settings

from . import errors as errors_mod
from . import observability, state
from .middleware import RequestIDMiddleware, SecurityHeadersMiddleware
from .routes import public_router, router

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

settings = get_settings()
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log.info("Starting up (env=%s, auth=%s, refresh=%dmin)",
             settings.env,
             "on" if settings.auth_required else "off",
             settings.pipeline_refresh_minutes)

    state.get_state()  # warm cache
    await state.start_refresh_task(settings.pipeline_refresh_minutes)
    log.info("Ready to serve")
    yield
    await state.stop_refresh_task()
    log.info("Shutting down")


# ---------------------------------------------------------------------------
# App + observability (Sentry must init before app construction)
# ---------------------------------------------------------------------------
observability.install_sentry(settings)

app = FastAPI(
    title="Transcript Intelligence API",
    version="1.0.0",
    description=(
        "Topic categorization, sentiment analysis, and strategic insights "
        "for B2B meeting transcripts.\n\n"
        f"**Environment:** `{settings.env}` · "
        f"**Auth:** {'X-API-Key required' if settings.auth_required else 'open (dev)'}"
    ),
    lifespan=lifespan,
)

errors_mod.register(app)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.rate_limit_default],
    headers_enabled=True,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, errors_mod.http_exception_handler)
app.add_middleware(SlowAPIASGIMiddleware)


# ---------------------------------------------------------------------------
# Cross-cutting middleware (order matters; outer-most listed first)
# ---------------------------------------------------------------------------
app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    SecurityHeadersMiddleware,
    hsts=settings.is_prod,  # only assert HSTS when actually behind TLS
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET"],
    allow_headers=["*", "X-API-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)


# ---------------------------------------------------------------------------
# Observability (must come after app construction)
# ---------------------------------------------------------------------------
observability.install_metrics(app, settings)
observability.install_tracing(app, settings)


# ---------------------------------------------------------------------------
# Routers — public (no auth) + v1 (auth-gated)
# ---------------------------------------------------------------------------
app.include_router(public_router)
app.include_router(router)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
if (WEB_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    for name in ("favicon.svg", "favicon.ico"):
        fpath = WEB_DIR / "static" / name
        if fpath.exists():
            return FileResponse(fpath)
    return FileResponse(WEB_DIR / "index.html", status_code=404)
