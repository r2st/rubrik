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
from pydantic import BaseModel

from api.csrf import require_csrf
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
_WRITE_LIMIT_PER_MINUTE = 60   # less aggressive — non-credential admin writes
_strict_window: dict[str, deque[float]] = defaultdict(deque)
_write_window: dict[str, deque[float]] = defaultdict(deque)


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


def admin_write_rate_limit(request: Request) -> None:
    """Less-aggressive rate limit for non-credential admin write endpoints
    (settings updates, snapshot rebuild, GDPR delete, outbox replay).

    60/min/IP — high enough that legitimate operator usage never bumps
    into it, low enough that a compromised-session attacker can't
    rapidly spam state changes. Same Redis-or-fallback pattern as
    ``strict_rate_limit``.
    """
    ip = (request.client.host if request.client else "unknown") or "unknown"
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
            key = f"admin_write_rl:{ip}"
            cnt = client.incr(key)
            if cnt == 1:
                client.expire(key, 60)
            if cnt > _WRITE_LIMIT_PER_MINUTE:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Admin write rate limit exceeded; pause and retry.",
                )
            return
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            pass

    now = time.monotonic()
    cutoff = now - 60.0
    bucket = _write_window[ip]
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _WRITE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Admin write rate limit exceeded; pause and retry.",
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

    # MFA enforcement — when TOTP is required, the password alone isn't
    # enough. The same 401 envelope is used so an attacker can't tell
    # whether they got the password right (no oracle).
    from .totp import is_totp_required, verify_login_code
    if is_totp_required() and not verify_login_code(req.totp):
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

    # CSRF token cookie — JS reads it (so NOT HttpOnly) and echoes via
    # X-CSRF-Token on every state-changing request. Server compares the
    # two via constant-time compare in api/csrf.py::require_csrf.
    from api.csrf import CSRF_COOKIE, issue_token
    response.set_cookie(
        key=CSRF_COOKIE,
        value=issue_token(),
        max_age=SESSION_TTL_SECONDS,
        httponly=False,                # JS reads it; that's the pattern
        samesite="strict",
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
    """Cheap session-validity probe for the UI. Also surfaces MFA state
    so the admin panel can show the correct TOTP card."""
    from .totp import is_totp_required
    return {"actor": actor, "totp_required": is_totp_required()}


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
def _to_setting_out(s) -> SettingOut:
    """Serialize a Setting row, masking the value when type is 'secret'.

    The raw secret never leaves the DB — operators see only "••••••<last 4>".
    To rotate, the UI sends a new full value via PUT; reading the value back
    always returns the masked form.
    """
    from src.runtime_settings import mask_secret
    value = mask_secret(s.value) if s.type == "secret" else s.value
    return SettingOut(
        key=s.key, value=value, type=s.type, category=s.category,
        description=s.description, updated_at=s.updated_at, updated_by=s.updated_by,
    )


@router.get("/settings", response_model=list[SettingsByCategory])
def list_settings(actor: str = Depends(require_admin)) -> list[SettingsByCategory]:
    grouped = get_runtime().by_category()
    out = []
    for category in sorted(grouped):
        if category.startswith("_"):  # internal — hide from UI
            continue
        items = [_to_setting_out(s) for s in grouped[category]]
        out.append(SettingsByCategory(category=category, settings=items))
    return out


@router.get("/settings/{key}", response_model=SettingOut)
def get_setting(key: str, actor: str = Depends(require_admin)) -> SettingOut:
    rs = get_runtime()
    matches = [s for s in rs.all() if s.key == key]
    if not matches:
        raise HTTPException(404, f"Setting {key!r} not found")
    return _to_setting_out(matches[0])


@router.put(
    "/settings/{key}",
    response_model=SettingOut,
    dependencies=[
        Depends(admin_write_rate_limit),
        Depends(require_csrf),
    ],
)
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
    return _to_setting_out(s)


@router.post(
    "/settings/{key}/reset",
    response_model=SettingOut,
    dependencies=[Depends(admin_write_rate_limit), Depends(require_csrf)],
)
def reset_setting(key: str, actor: str = Depends(require_admin)) -> SettingOut:
    try:
        s = get_runtime().reset(key, actor=actor)
    except KeyError as e:
        raise HTTPException(404, str(e)) from None
    return _to_setting_out(s)


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
# Outbox — operator surface for the transactional outbox / relayer (ADR 0014)
# ---------------------------------------------------------------------------
@router.get("/outbox/stuck")
def outbox_stuck_count(actor: str = Depends(require_admin)) -> dict:
    """Count rows the relayer gave up on (delivery_attempts ≥ max).

    Healthy = 0. Non-zero = downstream is sick or the publisher is
    misconfigured. See `deploy/incident-runbooks.md` §"Outbox relayer
    falling behind."
    """
    from api.outbox import count_stuck_rows
    return {"stuck": count_stuck_rows(), "actor": actor}


@router.post(
    "/outbox/replay",
    dependencies=[Depends(admin_write_rate_limit), Depends(require_csrf)],
)
def outbox_replay(
    limit: int = 100,
    actor: str = Depends(require_admin),
) -> dict:
    """Reset ``delivery_attempts`` to 0 for stuck rows so the relayer retries.

    Use after a publisher outage clears: rows that hit the attempt cap
    during the outage become eligible again on the next drain cycle. The
    relayer logs each retry; failed rows climb back to the cap and either
    route to the DLQ (if the publisher supports it) or stick again
    (operator inspects the source).
    """
    from api.outbox import replay_stuck_rows
    reset = replay_stuck_rows(limit=max(1, min(limit, 1000)))
    return {"ok": True, "reset": reset, "actor": actor}


# ---------------------------------------------------------------------------
# Snapshot — manually trigger a rebuild (off-path via Arq when available)
# ---------------------------------------------------------------------------
@router.post(
    "/snapshot/rebuild",
    dependencies=[Depends(admin_write_rate_limit), Depends(require_csrf)],
)
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


# ---------------------------------------------------------------------------
# TOTP / MFA (ADR 0006 — admin password is single-factor by default)
# ---------------------------------------------------------------------------
@router.post(
    "/totp/setup",
    dependencies=[Depends(admin_write_rate_limit), Depends(require_csrf)],
)
def totp_setup(actor: str = Depends(require_admin)) -> dict:
    """Mint a fresh TOTP secret + provisioning URI.

    The secret is NOT yet persisted — call ``/totp/verify`` with a code
    derived from this secret to commit. Aborted setups leave no trace.
    Returns ``{secret, uri, issuer, account}``; the UI renders the URI as
    a QR + shows the raw secret as a fallback.
    """
    from .totp import setup as _setup
    payload = _setup()
    return {**payload, "actor": actor}


class _TotpVerifyRequest(BaseModel):  # noqa: pydantic forwarded
    secret: str
    code: str


@router.post(
    "/totp/verify",
    dependencies=[Depends(admin_write_rate_limit), Depends(require_csrf)],
)
def totp_verify(
    req: _TotpVerifyRequest, actor: str = Depends(require_admin),
) -> dict:
    """Confirm the operator's authenticator computes valid codes for the
    secret returned by ``/totp/setup``. On success, the secret persists
    and ``auth.admin_totp_required`` flips to ``true``."""
    from .totp import verify_setup_code
    if not verify_setup_code(req.secret, req.code, actor=actor):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TOTP code did not match. Try again or restart setup.",
        )
    return {"ok": True, "totp_required": True, "actor": actor}


@router.post(
    "/totp/disable",
    dependencies=[Depends(admin_write_rate_limit), Depends(require_csrf)],
)
def totp_disable(actor: str = Depends(require_admin)) -> dict:
    """Clear the TOTP secret and lower the required flag.

    Used during recovery (lost authenticator). Logged in audit trail.
    Re-running ``/totp/setup`` re-establishes MFA.
    """
    from .totp import disable
    disable(actor=actor)
    return {"ok": True, "totp_required": False, "actor": actor}


# ---------------------------------------------------------------------------
# GDPR right-to-be-forgotten
# ---------------------------------------------------------------------------
class _GDPRDeleteRequest(BaseModel):  # noqa: pydantic forwarded
    customer_name: str
    confirmation: str    # must equal customer_name; soft-confirm guard


@router.post(
    "/gdpr/delete-customer",
    dependencies=[Depends(admin_write_rate_limit), Depends(require_csrf)],
)
def gdpr_delete_customer(
    req: _GDPRDeleteRequest, actor: str = Depends(require_admin),
) -> dict:
    """Delete every meeting belonging to a customer + emit downstream event.

    See ``deploy/gdpr-runbook.md`` for the operator procedure. The audit
    log records *that* a deletion happened with a hashed customer ID +
    deletion ID — never the raw customer name.
    """
    from .gdpr import GDPRConfirmationFailed, delete_customer
    try:
        return delete_customer(
            req.customer_name,
            confirmation=req.confirmation,
            actor=actor,
        )
    except GDPRConfirmationFailed as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
