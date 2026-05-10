"""TOTP-based MFA for the admin account.

Operator UX:

  1. Operator logs in with the bootstrap password.
  2. Operator hits ``POST /api/v1/admin/totp/setup`` — server generates a
     fresh base32 secret + a ``otpauth://`` provisioning URI. UI scans the
     QR (or pastes the secret) into Authenticator / 1Password / Bitwarden.
  3. Operator hits ``POST /api/v1/admin/totp/verify`` with the 6-digit
     code. On match, the secret persists into the ``auth.admin_totp_secret``
     runtime setting (masked-on-read ``secret`` type) and
     ``auth.admin_totp_required`` flips to ``true``.
  4. From here on, ``/api/v1/admin/login`` requires the ``totp`` field in
     the request body alongside the password.

Recovery / rotation:
  - Lost device? Operator with shell access can clear ``auth.admin_totp_*``
    via the runtime store (logged in the audit trail) and restart setup.
  - Backup codes are a follow-up — out of scope for this iteration; ADR
    0006's "When to revisit" notes them.

The TOTP secret is treated as a ``secret`` type — masked everywhere it's
read by the API, masked in audit-log entries, never round-trips through
the UI in plaintext after setup.
"""
from __future__ import annotations

from typing import Optional

import pyotp

from src.logging_config import get_logger
from src.runtime_settings import get_runtime

log = get_logger(__name__)

_ISSUER = "Transcript Intelligence"
_ACCOUNT = "admin"


def is_totp_required() -> bool:
    """True iff a TOTP secret is set AND the required flag is on."""
    rt = get_runtime()
    secret = str(rt.get("auth.admin_totp_secret", "") or "")
    required = bool(rt.get("auth.admin_totp_required", False))
    return bool(secret) and required


def setup() -> dict:
    """Mint a fresh secret. NOT yet persisted — caller stores it after
    ``verify_setup_code`` confirms the operator can compute valid codes.

    Returns the provisioning URI + base32 secret so the UI can render
    a QR + a fallback "type the secret manually" panel. The secret is
    held in-memory (in the response only); it lands in the DB exclusively
    via the verify step, so an aborted setup leaves no trace.
    """
    secret = pyotp.random_base32()
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=_ACCOUNT, issuer_name=_ISSUER,
    )
    return {"secret": secret, "uri": uri, "issuer": _ISSUER, "account": _ACCOUNT}


def verify_setup_code(secret: str, code: str, *, actor: str) -> bool:
    """Confirm the operator's authenticator computes valid codes for ``secret``.

    On success the secret is stored and ``admin_totp_required`` flips on.
    On failure nothing persists — caller should retry or restart setup.
    """
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        log.warning("TOTP setup verification failed (actor=%s)", actor)
        return False
    rt = get_runtime()
    rt.set("auth.admin_totp_secret", secret, actor=actor,
           notes="TOTP secret set via setup flow")
    rt.set("auth.admin_totp_required", True, actor=actor,
           notes="TOTP required after successful setup verification")
    log.info("TOTP setup completed (actor=%s)", actor)
    return True


def verify_login_code(code: Optional[str]) -> bool:
    """Validate a code against the stored secret. Returns False if no
    code was provided OR the code doesn't match."""
    if not code:
        return False
    rt = get_runtime()
    secret = str(rt.get("auth.admin_totp_secret", "") or "")
    if not secret:
        return False
    # ``valid_window=1`` accepts the previous + next 30 s window — covers
    # clock skew up to ±30 s without sliding the security floor.
    return bool(pyotp.TOTP(secret).verify(code, valid_window=1))


def disable(*, actor: str) -> None:
    """Clear the TOTP state — used by the rotation flow when the
    authenticator is lost. Only callable from an authenticated session."""
    rt = get_runtime()
    rt.set("auth.admin_totp_secret", "", actor=actor,
           notes="TOTP disabled / reset")
    rt.set("auth.admin_totp_required", False, actor=actor,
           notes="TOTP required-flag cleared")
    log.info("TOTP disabled (actor=%s)", actor)
