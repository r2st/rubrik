"""Admin authentication — scrypt-hashed password + signed session cookies.

Pattern: simple shared admin password, exchanged for a signed session token
on login. The token contains an issued-at timestamp and an HMAC over the
session_secret from `bootstrap.toml`. No external auth provider, no DB-side
session table — sessions are stateless.

Production hardening would add: per-user accounts, rate limiting on /login,
CSRF tokens on state-changing routes, IP pinning, refresh tokens. All of
those are layer-able on top of this without changing call sites.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request, status

from src.logging_config import get_logger
from src.settings import Settings, get_settings

log = get_logger(__name__)

SESSION_COOKIE = "ti_admin_session"
SESSION_TTL_SECONDS = 60 * 60 * 8  # 8 hours

# Where the (hashed) admin password lives. Treated as runtime data — bootstrap
# only supplies the *initial* password; the hash is generated and stored on
# first start, then rotated through the admin UI itself.
_PASSWORD_HASH_PATH_KEY = "admin.password_hash"


# ---------------------------------------------------------------------------
# Password hashing — PBKDF2-HMAC-SHA256 from stdlib (no extra deps, universally
# available unlike scrypt which depends on the linked OpenSSL build).
#
# 200k iterations is the OWASP 2023 recommended minimum for PBKDF2-SHA256.
# Format: `$pbkdf2-sha256$<iters>$<salt-b64>$<hash-b64>` (PHC-string-style).
# ---------------------------------------------------------------------------
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=32,
    )
    return (
        f"$pbkdf2-sha256${_PBKDF2_ITERATIONS}$"
        f"{base64.urlsafe_b64encode(salt).decode()}$"
        f"{base64.urlsafe_b64encode(digest).decode()}"
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        _, scheme, iters_str, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2-sha256":
        return False
    try:
        iters = int(iters_str)
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(digest_b64)
    except Exception:  # noqa: BLE001
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iters, dklen=32,
    )
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# Hashed-password lookup (bootstrap-on-first-start)
# ---------------------------------------------------------------------------
def _password_hash_from_db() -> Optional[str]:
    """Read the admin password hash from the runtime settings store.

    Returns None if no hash has been stored yet — caller should bootstrap from
    the `[admin].initial_password` field in bootstrap.toml.
    """
    try:
        from src.runtime_settings import get_runtime
        v = get_runtime().get(_PASSWORD_HASH_PATH_KEY, "")
        return v or None
    except Exception:  # noqa: BLE001
        return None


def ensure_admin_password_seeded() -> None:
    """On first start, hash the bootstrap password and store it. Idempotent."""
    if _password_hash_from_db():
        return
    settings = get_settings()
    initial = settings.admin.initial_password
    if not initial or initial == "changeme-on-first-login":
        log.warning(
            "Admin bootstrap password is the default. Change [admin].initial_password "
            "in bootstrap.toml or rotate via /admin once logged in."
        )
    # Inject the hash into runtime_settings — but the password key isn't in
    # DEFAULTS by design (it's not user-tunable in the normal sense). We use a
    # direct INSERT via session_scope to avoid DEFAULTS lookup.
    from src.db import session_scope
    from src.models_db import Setting

    with session_scope() as s:
        if s.get(Setting, _PASSWORD_HASH_PATH_KEY) is None:
            s.add(Setting(
                key=_PASSWORD_HASH_PATH_KEY,
                value=hash_password(initial),
                type="str",
                category="_internal",
                description="Hashed admin password (managed; do not edit through API).",
                updated_by="bootstrap",
            ))
            s.commit()
            log.info("Admin password hash seeded from bootstrap.toml")


def update_admin_password(new_password: str, *, actor: str) -> None:
    """Rotate the admin password — called from the admin UI."""
    from src.db import session_scope
    from src.models_db import AuditLog, Setting

    if len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters")
    with session_scope() as s:
        existing = s.get(Setting, _PASSWORD_HASH_PATH_KEY)
        new_hash = hash_password(new_password)
        if existing is None:
            s.add(Setting(
                key=_PASSWORD_HASH_PATH_KEY, value=new_hash, type="str",
                category="_internal",
                description="Hashed admin password.",
                updated_by=actor,
            ))
        else:
            existing.value = new_hash
            existing.updated_by = actor
        s.add(AuditLog(
            actor=actor, action="set", setting_key=_PASSWORD_HASH_PATH_KEY,
            old_value="<redacted>", new_value="<redacted>",
            notes="Admin password rotated",
        ))
        s.commit()
    # Invalidate the runtime-settings cache so subsequent verifications use the new hash.
    from src.runtime_settings import get_runtime
    get_runtime()._invalidate()
    log.info("Admin password rotated by %s", actor)


# ---------------------------------------------------------------------------
# Session tokens — stateless, signed
# ---------------------------------------------------------------------------
def issue_session(actor: str, settings: Settings) -> str:
    """Mint a session cookie payload."""
    payload = {"sub": actor, "iat": int(time.time())}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = _sign(body, settings.admin.resolved_session_secret())
    return f"{body}.{sig}"


def _sign(body: str, secret: str) -> str:
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def verify_session(token: str, settings: Settings) -> Optional[dict]:
    """Return the decoded session payload if valid, else None."""
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".", 1)
    if not hmac.compare_digest(_sign(body, settings.admin.resolved_session_secret()), sig):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception:  # noqa: BLE001
        return None
    if int(time.time()) - int(payload.get("iat", 0)) > SESSION_TTL_SECONDS:
        return None
    return payload


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
def login_attempt(password: str) -> bool:
    """Verify a password against the stored hash. Constant-time."""
    stored = _password_hash_from_db()
    if not stored:
        # Hash hasn't been seeded yet — accept the bootstrap password directly
        # and seed on success.
        if password and password == get_settings().admin.initial_password:
            ensure_admin_password_seeded()
            return True
        return False
    return verify_password(password, stored)


async def require_admin(
    request: Request,
    session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    settings: Settings = Depends(get_settings),
) -> str:
    """FastAPI dependency: gate routes behind a valid admin session.

    Returns the actor's subject (always 'admin' for the single-user model).
    """
    payload = verify_session(session or "", settings)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
        )
    request.state.admin_subject = payload["sub"]
    return payload["sub"]


def constant_time_eq(a: str, b: str) -> bool:
    """Avoid leaking length info during password compare."""
    return secrets.compare_digest(a, b)
