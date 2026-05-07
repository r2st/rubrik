"""Admin API — runtime settings CRUD + audit log + password rotation.

Mounted at `/api/v1/admin/*`. Every route requires a valid admin session
(except `POST /login`).

`/login` and `/password` are rate-limited more aggressively than the rest of
the API to discourage brute-force attempts (5/minute/IP via the
`strict_rate_limit` dependency). The app-wide slowapi middleware applies
its global limit on top, so an attacker hits the strict bound first.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from src.runtime_settings import get_runtime
from src.settings import Settings, get_settings

from .auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    _password_hash_from_db,  # noqa: F401  — used by change_password
    issue_session,
    login_attempt,
    require_admin,
    update_admin_password,
    verify_password,
)
from .schemas import (
    AuditEntry,
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    SettingOut,
    SettingsByCategory,
    UpdateSettingRequest,
)

# Strict rate limit for credential-handling endpoints (login + password
# rotation): 5 attempts / minute / IP. Implemented as a tiny in-memory
# sliding window so it's a plain FastAPI dependency — slowapi's decorator
# wrapping interferes with FastAPI body-model introspection on newer
# pydantic, so we keep the route signatures clean and cap requests here.
#
# In multi-replica deployments this is per-process, which is fine: an
# attacker is still bounded to (replicas × 5)/minute, and the global
# slowapi middleware applies on top. For a stricter cluster-wide bound,
# move the counter to Redis.
_STRICT_LIMIT_PER_MINUTE = 5
_strict_window: dict[str, deque[float]] = defaultdict(deque)


def strict_rate_limit(request: Request) -> None:
    """Enforce 5 req/min/IP on login + password-rotation.

    Tries Redis first (cluster-wide enforcement) and falls back to a
    per-process sliding window. Without Redis, an attacker hitting 10
    replicas gets 10× the limit — fine for single-replica dev, broken at
    real scale. Redis is the production path.
    """
    ip = (request.client.host if request.client else "unknown") or "unknown"

    # ---- cluster-wide path (Redis-backed) ----
    redis_url = None
    try:
        from src.settings import get_settings  # noqa: PLC0415
        redis_url = get_settings().redis_url
    except Exception:  # noqa: BLE001
        pass
    if redis_url:
        try:
            import redis as _redis  # noqa: PLC0415
            client = _redis.Redis.from_url(redis_url, socket_timeout=0.25)
            key = f"strict_rl:{ip}"
            cnt = client.incr(key)
            if cnt == 1:
                client.expire(key, 60)
            if cnt > _STRICT_LIMIT_PER_MINUTE:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many attempts; please wait a minute before retrying.",
                )
            return
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — degrade to in-process
            pass

    # ---- in-process fallback ----
    now = time.monotonic()
    cutoff = now - 60.0
    bucket = _strict_window[ip]
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _STRICT_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts; please wait a minute before retrying.",
        )
    bucket.append(now)


router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@router.post("/login", response_model=LoginResponse,
             dependencies=[Depends(strict_rate_limit)])
def login(req: LoginRequest, response: Response,
          settings: Settings = Depends(get_settings)) -> LoginResponse:
    if not login_attempt(req.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    token = issue_session("admin", settings)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        # `secure` is true in prod (TLS) but false in dev so localhost works
        secure=settings.is_prod,
        path="/",
    )
    return LoginResponse(ok=True)


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(actor: str = Depends(require_admin)) -> dict:
    """Cheap session-validity probe for the UI."""
    return {"actor": actor}


@router.post("/password", dependencies=[Depends(strict_rate_limit)])
def change_password(
    req: PasswordChangeRequest,
    actor: str = Depends(require_admin),
) -> dict:
    stored = _password_hash_from_db()
    if not stored or not verify_password(req.current_password, stored):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    update_admin_password(req.new_password, actor=actor)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@router.get("/settings", response_model=list[SettingsByCategory])
def list_settings(actor: str = Depends(require_admin)) -> list[SettingsByCategory]:
    grouped = get_runtime().by_category()
    out = []
    for category in sorted(grouped):
        if category.startswith("_"):  # internal — hide from UI
            continue
        items = [
            SettingOut(
                key=s.key, value=s.value, type=s.type,
                category=s.category, description=s.description,
                updated_at=s.updated_at, updated_by=s.updated_by,
            )
            for s in grouped[category]
        ]
        out.append(SettingsByCategory(category=category, settings=items))
    return out


@router.get("/settings/{key}", response_model=SettingOut)
def get_setting(key: str, actor: str = Depends(require_admin)) -> SettingOut:
    rs = get_runtime()
    matches = [s for s in rs.all() if s.key == key]
    if not matches:
        raise HTTPException(404, f"Setting {key!r} not found")
    s = matches[0]
    return SettingOut(
        key=s.key, value=s.value, type=s.type, category=s.category,
        description=s.description, updated_at=s.updated_at, updated_by=s.updated_by,
    )


@router.put("/settings/{key}", response_model=SettingOut)
def update_setting(
    key: str, req: UpdateSettingRequest,
    actor: str = Depends(require_admin),
) -> SettingOut:
    try:
        s = get_runtime().set(key, req.value, actor=actor, notes=req.notes)
    except KeyError as e:
        raise HTTPException(404, str(e)) from None
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"Invalid value: {e}") from None
    return SettingOut(
        key=s.key, value=s.value, type=s.type, category=s.category,
        description=s.description, updated_at=s.updated_at, updated_by=s.updated_by,
    )


@router.post("/settings/{key}/reset", response_model=SettingOut)
def reset_setting(key: str, actor: str = Depends(require_admin)) -> SettingOut:
    try:
        s = get_runtime().reset(key, actor=actor)
    except KeyError as e:
        raise HTTPException(404, str(e)) from None
    return SettingOut(
        key=s.key, value=s.value, type=s.type, category=s.category,
        description=s.description, updated_at=s.updated_at, updated_by=s.updated_by,
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
@router.get("/audit", response_model=list[AuditEntry])
def get_audit(
    limit: int = 100,
    actor: str = Depends(require_admin),
) -> list[AuditEntry]:
    rows = get_runtime().audit_log(limit=limit)
    return [
        AuditEntry(
            id=r.id, timestamp=r.timestamp, actor=r.actor, action=r.action,
            setting_key=r.setting_key,
            old_value=r.old_value, new_value=r.new_value, notes=r.notes,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Snapshot — manually trigger a rebuild (off-path via Arq when available)
# ---------------------------------------------------------------------------
@router.post("/snapshot/rebuild")
async def rebuild_snapshot_endpoint(actor: str = Depends(require_admin)) -> dict:
    """Kick a snapshot rebuild.

    Tries to enqueue an Arq job (off-path; worker pool runs it) so the API
    process isn't blocked. Falls back to inline execution when Redis / Arq
    aren't configured — useful for single-replica dev.
    """
    from api.jobs import enqueue
    result = await enqueue("rebuild_snapshot")
    if result.enqueued:
        return {"ok": True, "mode": "queued", "job_id": result.job_id}

    # No queue available — run inline (this is the dev fallback).
    from api import jobs
    payload = await jobs.rebuild_snapshot({}, url=None)
    return {"ok": True, "mode": "inline", "result": payload, "actor": actor,
            "queue_skip_reason": result.reason}
