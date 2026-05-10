"""GDPR-style customer-data deletion.

Implements the right-to-be-forgotten path. Three things happen
atomically (one DB transaction, one outbox event):

  1. **Delete from the canonical store** — every meeting belonging to
     the customer is removed from whichever ``TranscriptRepository`` is
     currently configured. ``LocalDirectoryRepository`` doesn't expose a
     delete (filesystem is read-only in this codebase); ``DatabaseRepository``
     gets a typed ``DELETE``.
  2. **Emit an outbox event** — ``customer.deleted`` with the customer
     name + a deterministic deletion ID. Downstream consumers (ClickHouse,
     OpenSearch, Iceberg snapshots, search index) honour by deleting their
     copies. Backfilled retroactive deletes use the same path.
  3. **Audit log a redacted record** — the audit row records *that* a
     deletion happened + which actor + how many rows + the deletion ID,
     but NOT the customer name. The name is hashed; the operator can
     prove the deletion ran without re-storing the data they just deleted.

The ``confirmation`` parameter is required to equal the customer name —
prevents the accidental "rm -rf customer". Soft-confirm pattern.

What this DOES NOT do (deliberate):
  - Doesn't touch the ``audit_log`` historical entries that mention the
    customer in ``setting_key`` or ``notes``. Audit logs are append-only
    by design (compliance-required); the deletion entry sits next to
    them and a downstream processor can scrub historical references in a
    separate pass if the regulatory regime requires it.
  - Doesn't delete LLM Tier-2 cached responses keyed on a redacted
    prompt — they're already PII-redacted before caching.
  - Doesn't delete from training-set Iceberg files — those are immutable
    history; the next training run filters via the deletion-ID journal.

Operator runbook: ``deploy/gdpr-runbook.md``.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any

from sqlalchemy import text

from api.outbox import emit
from src.db import session_scope
from src.logging_config import get_logger
from src.models_db import AuditLog
from src.repository import DatabaseRepository

log = get_logger(__name__)


class GDPRConfirmationFailed(Exception):
    """Raised when ``confirmation`` doesn't match the customer name."""


def delete_customer(
    customer_name: str,
    *,
    confirmation: str,
    actor: str,
    table_name: str = "meetings",
) -> dict[str, Any]:
    """Run the deletion. Returns counts + a deletion ID for audit.

    Raises ``GDPRConfirmationFailed`` if the soft-confirm doesn't match.
    """
    if customer_name != confirmation:
        raise GDPRConfirmationFailed(
            "confirmation must equal customer_name to proceed"
        )

    deletion_id = secrets.token_hex(8)
    customer_hash = hashlib.sha256(
        customer_name.encode("utf-8"),
    ).hexdigest()[:16]

    deleted_meetings = 0

    # Best-effort delete from the canonical store. We try the database
    # backend first (production); local filesystem is intentionally
    # read-only in this codebase, so seeding-level deletion goes through
    # the upstream pipeline instead.
    try:
        with session_scope() as s:
            # Structured JSONB match on the canonical customer field —
            # NOT a free-text LIKE. A LIKE could delete unrelated rows
            # whose title happened to contain the customer name as a
            # substring (and a single-char customer name would scorch
            # the table). The customer name lives in
            # ``raw->'info'->>'customer'`` for everything data_loader
            # writes; the SQLite dev path uses ``json_extract`` so the
            # same logical query works on both backends.
            is_pg = s.bind.dialect.name == "postgresql"
            if is_pg:
                stmt = text(
                    f"DELETE FROM {table_name} "
                    f"WHERE raw->'info'->>'customer' = :name"
                )
            else:
                stmt = text(
                    f"DELETE FROM {table_name} "
                    f"WHERE json_extract(raw, '$.info.customer') = :name"
                )
            result = s.execute(stmt, {"name": customer_name})
            deleted_meetings = int(result.rowcount or 0)

            # Emit the outbox event in the SAME transaction as the
            # delete — either both land or neither does, no orphaned
            # downstream propagation.
            emit(
                s,
                aggregate_type="customer",
                aggregate_id=customer_hash,
                event_type="customer.deleted",
                payload={
                    "customer_hash": customer_hash,
                    "deletion_id": deletion_id,
                    "deleted_meetings": deleted_meetings,
                    "actor": actor,
                },
            )

            # Audit row — hashed customer name, NOT the raw value.
            s.add(AuditLog(
                actor=actor,
                action="gdpr_delete",
                setting_key=None,
                new_value={
                    "customer_hash": customer_hash,
                    "deletion_id": deletion_id,
                    "deleted_meetings": deleted_meetings,
                    "table": table_name,
                },
                notes=(
                    "GDPR right-to-be-forgotten executed; "
                    "customer name redacted from this row"
                ),
            ))
            s.commit()
    except Exception:  # noqa: BLE001
        log.exception(
            "GDPR delete failed (actor=%s, deletion_id=%s)",
            actor, deletion_id,
        )
        raise

    # Pipeline cache — a deleted customer should disappear from
    # PipelineState within the next refresh cycle. Force an invalidation
    # so the dashboard never serves stale data after deletion.
    try:
        from api import state
        state.reload()
    except Exception:  # noqa: BLE001 — best effort
        log.exception("PipelineState invalidate after GDPR delete failed")

    log.info(
        "GDPR delete completed: actor=%s deletion_id=%s "
        "customer_hash=%s deleted_meetings=%d",
        actor, deletion_id, customer_hash, deleted_meetings,
    )
    return {
        "ok": True,
        "deletion_id": deletion_id,
        "customer_hash": customer_hash,
        "deleted_meetings": deleted_meetings,
        "outbox_event_emitted": True,
        "actor": actor,
    }


# Re-export for convenience.
__all__ = ["delete_customer", "GDPRConfirmationFailed"]
_ = DatabaseRepository  # silence unused-import warnings; future calls use it
