"""Standalone admin-panel FastAPI app.

The public API and the admin panel are intentionally **two separate
processes** in production: different blast radius, different threat model,
different traffic shape. The Gateway API HTTPRoute (`deploy/k8s/gateway.yaml`)
routes ``/api/v1/admin/*`` to *this* app on its own listener, while
``/api/v1/*`` (analyst / public read API) goes to ``api.main`` on its own.

Run::

    uvicorn api.admin_app:app --host 127.0.0.1 --port 8001

What lives here:
  - ``/admin``           — the admin HTML page
  - ``/api/v1/admin/*``  — the admin REST API (settings CRUD, audit log,
                            login/logout, password rotation, snapshot rebuild)
  - ``/api/live``        — liveness probe
  - ``/api/ready``       — readiness probe (DB only — no pipeline cache)
  - ``/static/*``        — static assets the admin page needs

What does **not** live here (it's on the public API process instead):
  - ``/api/v1/summary``, ``/api/v1/meetings``, etc. — analyst read API
  - the PipelineState cache + refresh task + snapshot poll
  - the public dashboard at ``/``

Why split: admin traffic is low-volume, security-sensitive, and operator-
only; analyst traffic is high-volume, cacheable, and public. Putting them
on different ports lets the platform team route them through different
Gateway listeners (admin on a private VLAN; public via the CDN), apply
different rate-limit budgets, and scale each independently. ADR 0014
§"Control plane vs. data plane" documents the rationale.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.db import get_engine
from src.logging_config import configure_logging, get_logger
from src.runtime_settings import (
    get_runtime,
    initialize_db_and_seed,
    start_listener,
)
from src.settings import get_settings

from . import errors as errors_mod
from . import idempotency as idempotency_mod
from .admin.auth import ensure_admin_password_seeded
from .admin.routes import router as admin_router
from .middleware import (
    BodySizeLimitMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

settings = get_settings()
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — DB seed + LISTEN/NOTIFY listener; no pipeline cache.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app_: FastAPI):
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    app_.state.shutting_down = False

    initialize_db_and_seed()
    ensure_admin_password_seeded()

    log.info("Admin app starting up (env=%s)", settings.env)

    listener = start_listener(lambda _payload: get_runtime()._invalidate())  # noqa: SLF001

    log.info("Admin app ready to serve")
    yield

    log.info("Admin app shutting down — initiating drain")
    app_.state.shutting_down = True
    await asyncio.sleep(2)
    if listener is not None:
        listener.stop()
    try:
        get_engine().dispose()
    except Exception:  # noqa: BLE001
        log.exception("Engine dispose during shutdown failed")
    log.info("Admin app shutdown complete")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
# Bootstrap DB before observability or middleware reads runtime settings.
initialize_db_and_seed()
ensure_admin_password_seeded()

app = FastAPI(
    title="Transcript Intelligence — Admin",
    version="1.0.0",
    description=(
        "Admin panel — settings, audit log, password rotation, snapshot "
        "rebuild. Operator-only; deploy on a private listener.\n\n"
        f"**Environment:** `{settings.env}`."
    ),
    lifespan=lifespan,
)

errors_mod.register(app)

# Admin traffic is low-volume — no backpressure or adaptive throttle here.
# But body-size + request-id + security headers still apply.
app.add_middleware(BodySizeLimitMiddleware)
# Idempotency-Key cache — admin endpoints (password rotation, snapshot
# rebuild, settings updates) are exactly where client retries cause
# duplicate writes; the cache makes them safe.
app.add_middleware(idempotency_mod.IdempotencyMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_prod)


# Forensic context — every audit row written during this request will
# carry the caller's IP + user-agent. Set as a tiny ASGI middleware so it
# runs before any route handler can issue an AuditLog write.
@app.middleware("http")
async def _audit_context_middleware(request, call_next):
    from src.runtime_settings import set_audit_context
    # X-Forwarded-For wins when present (the deploy puts Caddy / a CDN
    # in front of the admin app); fall back to the socket peer.
    xff = request.headers.get("x-forwarded-for")
    ip = xff.split(",")[0].strip() if xff else (
        request.client.host if request.client else None
    )
    ua = request.headers.get("user-agent")
    set_audit_context(ip=ip, user_agent=ua[:256] if ua else None)
    return await call_next(request)

# CORS — admin panel UI is same-origin in the typical deploy, but a
# tight allowlist is cheap insurance.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_runtime().get("auth.cors_origins", ["*"]) or ["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*", "X-Request-ID"],
    allow_credentials=True,
    expose_headers=["X-Request-ID"],
)


# ---------------------------------------------------------------------------
# Probes — same shape as the public API so platform tooling is consistent.
# ---------------------------------------------------------------------------
@app.get("/api/live", include_in_schema=False)
def liveness() -> dict:
    """Process up; event loop responsive. No DB call."""
    return {"status": "alive"}


@app.get("/api/ready", include_in_schema=False)
def readiness(request: Request) -> JSONResponse:
    """Ready when the admin DB is reachable and we're not draining."""
    draining = bool(getattr(request.app.state, "shutting_down", False))
    db_ok = True
    try:
        from src.db import session_scope
        from src.models_db import Setting
        with session_scope() as s:
            s.query(Setting).limit(1).all()
    except Exception:  # noqa: BLE001
        db_ok = False
    ready = db_ok and not draining
    return JSONResponse(
        status_code=(200 if ready else 503),
        content={
            "status": "ready" if ready else "not_ready",
            "checks": {"db_reachable": db_ok, "not_draining": not draining},
        },
    )


# ---------------------------------------------------------------------------
# Admin router — /api/v1/admin/login, /me, /settings, /audit, etc.
# ---------------------------------------------------------------------------
app.include_router(admin_router)


# ---------------------------------------------------------------------------
# Static admin page + supporting assets
# ---------------------------------------------------------------------------
if (WEB_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def admin_index() -> FileResponse:
    """Serve the admin HTML at the root of this listener.

    The public dashboard lives on the OTHER process — at ``/`` of the
    public API listener. On this admin listener, the root IS the admin
    panel.
    """
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    """Backward-compat alias for /admin → admin.html."""
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    for name in ("favicon.svg", "favicon.ico"):
        fpath = WEB_DIR / "static" / name
        if fpath.exists():
            return FileResponse(fpath)
    return FileResponse(WEB_DIR / "admin.html", status_code=404)
