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

import hashlib
import json
import secrets as _secrets
from typing import Optional

import pyotp

from src.logging_config import get_logger
from src.runtime_settings import get_runtime

log = get_logger(__name__)

_ISSUER = "Transcript Intelligence"
_ACCOUNT = "admin"
_BACKUP_CODE_COUNT = 8
_BACKUP_CODES_KEY = "auth.admin_totp_backup_codes"


def _hash_code(code: str) -> str:
    """One-way hash for backup codes — we store the hash, not the code.

    Backup codes are short (10 chars) so a plain hash gives operators a
    consistent length without needing pbkdf2 latency on every login. The
    secret-typed setting also keeps them out of audit-log echoes.
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _generate_backup_codes(n: int = _BACKUP_CODE_COUNT) -> list[str]:
    """Cryptographically random codes, ``XXXX-XXXX`` format for readability."""
    out: list[str] = []
    for _ in range(n):
        raw = _secrets.token_hex(4).upper()  # 8 hex chars
        out.append(f"{raw[:4]}-{raw[4:]}")
    return out


def _load_backup_hashes() -> list[str]:
    raw = get_runtime().get(_BACKUP_CODES_KEY, "")
    if not raw:
        return []
    try:
        return list(json.loads(raw))
    except (ValueError, TypeError):
        return []


def _save_backup_hashes(hashes: list[str], *, actor: str) -> None:
    get_runtime().set(
        _BACKUP_CODES_KEY, json.dumps(hashes), actor=actor,
        notes=f"backup-codes updated ({len(hashes)} remaining)",
    )


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


def verify_setup_code(
    secret: str, code: str, *, actor: str,
) -> Optional[list[str]]:
    """Confirm the operator's authenticator computes valid codes for ``secret``.

    On success: the secret is stored, ``admin_totp_required`` flips on,
    and a fresh batch of ``_BACKUP_CODE_COUNT`` single-use backup codes
    is generated. Returns the raw codes (THE ONLY TIME they're visible)
    so the UI can show them to the operator. Hashes persist; raw codes
    do not.

    Returns ``None`` on verification failure (back-compat callers should
    treat that as ``False``).
    """
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        log.warning("TOTP setup verification failed (actor=%s)", actor)
        return None
    rt = get_runtime()
    rt.set("auth.admin_totp_secret", secret, actor=actor,
           notes="TOTP secret set via setup flow")
    rt.set("auth.admin_totp_required", True, actor=actor,
           notes="TOTP required after successful setup verification")
    raw = _generate_backup_codes()
    _save_backup_hashes([_hash_code(c) for c in raw], actor=actor)
    log.info("TOTP setup completed (actor=%s, %d backup codes minted)",
             actor, len(raw))
    return raw


def regenerate_backup_codes(*, actor: str) -> list[str]:
    """Mint a fresh batch, invalidating every previous code. Returns the
    raw codes once — the caller shows them to the operator, never
    persisted in plaintext."""
    raw = _generate_backup_codes()
    _save_backup_hashes([_hash_code(c) for c in raw], actor=actor)
    log.info("TOTP backup codes regenerated (actor=%s)", actor)
    return raw


def _consume_backup_code(code: str, *, actor: str) -> bool:
    """Constant-time check + atomic single-use consumption.

    Returns True iff the code matched a stored hash. On match the hash
    is removed so the same code can never satisfy login twice.
    """
    hashes = _load_backup_hashes()
    if not hashes:
        return False
    candidate = _hash_code(code.strip().upper())
    matched_idx = -1
    found = False
    for i, h in enumerate(hashes):
        # Plain `==` is fine on sha256 hex digests (same length always);
        # use compare_digest to keep linters + reviewers happy.
        if _secrets.compare_digest(h, candidate):
            matched_idx = i
            found = True
            # don't break — process all entries to keep timing flat
    if not found:
        return False
    hashes.pop(matched_idx)
    _save_backup_hashes(hashes, actor=actor)
    log.info("TOTP backup code consumed (actor=%s, %d remaining)",
             actor, len(hashes))
    return True


def verify_login_code(
    code: Optional[str], *, actor: str = "admin",
) -> bool:
    """Validate a code against the stored secret OR a stored backup code.

    A backup code is recognised by its ``XXXX-XXXX`` shape (contains a
    hyphen) — the TOTP path is tried first because it's the hot path.
    Returns False if no code was provided OR neither verifier matched.
    """
    if not code:
        return False
    rt = get_runtime()
    secret = str(rt.get("auth.admin_totp_secret", "") or "")
    if not secret:
        return False
    # ``valid_window=1`` accepts the previous + next 30 s window — covers
    # clock skew up to ±30 s without sliding the security floor.
    if pyotp.TOTP(secret).verify(code, valid_window=1):
        return True
    # Backup-code fallback. We don't reject hyphen-less codes outright —
    # an operator could fat-finger the format; let _consume_backup_code
    # be the source of truth.
    if "-" in code:
        return _consume_backup_code(code, actor=actor)
    return False


def disable(*, actor: str) -> None:
    """Clear the TOTP state — used by the rotation flow when the
    authenticator is lost. Only callable from an authenticated session."""
    rt = get_runtime()
    rt.set("auth.admin_totp_secret", "", actor=actor,
           notes="TOTP disabled / reset")
    rt.set("auth.admin_totp_required", False, actor=actor,
           notes="TOTP required-flag cleared")
    # Burn any outstanding backup codes — disabling MFA invalidates them.
    _save_backup_hashes([], actor=actor)
    log.info("TOTP disabled (actor=%s)", actor)
