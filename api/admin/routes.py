"""Admin API — runtime settings CRUD + audit log + password rotation.

Mounted at `/api/v1/admin/*`. Every route requires a valid admin session
(except `POST /login`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

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

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@router.post("/login", response_model=LoginResponse)
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


@router.post("/password")
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
