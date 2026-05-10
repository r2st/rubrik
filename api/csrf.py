"""CSRF protection for the admin app — double-submit cookie + Sec-Fetch-Site.

Why both: ``Sec-Fetch-Site`` is the modern-browser primary defence (rejects
cross-origin state-changing requests at the metadata level), but it's a
hint, not a guarantee — older clients and non-browser tooling don't send
it. The double-submit cookie is the belt to the suspenders: a CSRF token
issued at login lives in a non-``HttpOnly`` cookie that the JS reads and
echoes as the ``X-CSRF-Token`` header. Server compares the two via
``hmac.compare_digest``.

Pattern:

  1. ``POST /api/v1/admin/login`` issues both the session cookie AND a
     ``csrf_token`` cookie (NOT HttpOnly so JS can read it; SameSite=Strict
     keeps it from sneaking out to other sites).
  2. The admin UI's ``fetch()`` wrapper reads the cookie and sets the
     ``X-CSRF-Token`` header on every state-changing request.
  3. The server-side ``require_csrf`` dependency runs on every admin write
     route. It rejects when:
       - ``Sec-Fetch-Site`` is ``cross-site`` (most likely a CSRF attack)
       - The header / cookie token pair is missing or doesn't match
     Read endpoints (``GET`` / ``HEAD``) skip — they aren't state-changing.

Operator surface:
  - ``auth.csrf_enabled`` runtime setting (bool, default true). Off = no
    enforcement, useful for migration windows when the UI hasn't shipped
    the header yet.
"""
from __future__ import annotations

import hmac
import secrets
from typing import Optional

from fastapi import HTTPException, Request, status

CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
_CROSS_SITE = "cross-site"
_TOKEN_BYTES = 32


def issue_token() -> str:
    """Mint a new CSRF token. Call this on login (or session refresh)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _csrf_enabled() -> bool:
    try:
        from src.runtime_settings import get_runtime
        return bool(get_runtime().get("auth.csrf_enabled", True))
    except Exception:  # noqa: BLE001
        return True


def _read_token_pair(request: Request) -> tuple[Optional[str], Optional[str]]:
    return (
        request.cookies.get(CSRF_COOKIE),
        request.headers.get(CSRF_HEADER),
    )


async def require_csrf(request: Request) -> None:
    """FastAPI dependency — rejects forged cross-origin write attempts.

    Order of defences:
      1. If ``Sec-Fetch-Site: cross-site`` → reject immediately.
      2. Otherwise require ``cookie == header`` via constant-time compare.
    """
    if not _csrf_enabled():
        return

    if request.method in ("GET", "HEAD", "OPTIONS"):
        return  # CSRF doesn't apply to safe methods

    sec_fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
    if sec_fetch_site == _CROSS_SITE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Cross-site request blocked by CSRF protection "
                "(Sec-Fetch-Site=cross-site)."
            ),
        )

    cookie, header = _read_token_pair(request)
    if not cookie or not header:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Missing CSRF token. Login establishes the cookie + the UI "
                "echoes it via the X-CSRF-Token header."
            ),
        )
    if not hmac.compare_digest(cookie.encode("utf-8"), header.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token mismatch.",
        )
