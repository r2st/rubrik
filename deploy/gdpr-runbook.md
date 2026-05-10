# GDPR runbook — right-to-be-forgotten

Operator procedure for satisfying a GDPR Article 17 deletion request (or
the equivalent under CCPA / LGPD / CPRA / similar regimes).

The implementation lives in `api/admin/gdpr.py`; the operator-facing
endpoint is `POST /api/v1/admin/gdpr/delete-customer`. Both go through
the standard admin auth + write-rate-limit + CSRF dependencies.

## Before you run a deletion

1. **Verify the request is in scope.** Confirm with Legal that the
   request actually requires deletion (vs. anonymisation or erasure of
   a specific subset). Some regulators require deletion only of the
   personal-data fields, not entire transactional records.
2. **Snapshot the audit trail.** Capture `audit_log` rows referencing
   the customer name BEFORE the deletion runs — once the deletion
   completes, the new audit row only carries a hashed customer ID, so
   the "what data did we have?" question gets harder to answer.
3. **Notify downstream sinks.** ClickHouse, Iceberg, OpenSearch, and
   any third-party processors honour the `customer.deleted` outbox
   event automatically. But confirm consumers are alive (KEDA-scaled
   workers, search indexer) — otherwise the event sits in the topic
   until they catch up.

## Run the deletion

```bash
# 1. Authenticate as admin (in the admin panel or via curl)
curl -c /tmp/cookies -b /tmp/cookies \
  -X POST https://admin.example.com/api/v1/admin/login \
  -H "Content-Type: application/json" \
  -d '{"password":"...","totp":"123456"}'

# 2. Read the CSRF token cookie that login set
CSRF=$(awk '/csrf_token/{print $7}' /tmp/cookies)

# 3. Run the deletion. confirmation MUST equal customer_name (soft-confirm).
curl -b /tmp/cookies \
  -X POST https://admin.example.com/api/v1/admin/gdpr/delete-customer \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"customer_name":"Acme Corp","confirmation":"Acme Corp"}'
```

Successful response:

```json
{
  "ok": true,
  "deletion_id": "9f1a3c2d4e5f6b78",
  "customer_hash": "9c3f5d2a8b4e6f1c",
  "deleted_meetings": 47,
  "outbox_event_emitted": true,
  "actor": "admin"
}
```

## What just happened (in one transaction)

1. **Postgres** — every row in `meetings` whose JSON blob mentions the
   customer name is deleted. `rowcount` is returned to the operator.
2. **Outbox event** — a `customer.deleted` row lands in `outbox_events`
   in the same transaction. Either both succeed or both roll back. The
   event payload carries the hashed customer ID + deletion ID + count;
   no raw customer name leaves the API process.
3. **Audit log** — a row records actor, action, deletion ID, count,
   table. `setting_key` is null; `notes` records "GDPR right-to-be-
   forgotten executed; customer name redacted from this row."
4. **Pipeline cache** — `state.reload()` runs after commit so the
   dashboard never serves stale data referencing the deleted customer.
5. **Downstream** — the relayer publishes the outbox event to the
   event backbone; ClickHouse / Iceberg / OpenSearch consumers process
   their copies and emit confirmation events back. A separate operator
   verification step waits for those acks (TODO: ship a verification
   helper).

## After the deletion

1. **Verify counts.** The returned `deleted_meetings` should match a
   pre-deletion count from `validate.py`. If it's lower, some rows
   weren't matched (e.g. customer-name normalization differences) —
   investigate before signing off.
2. **Verify downstream processing.** Tail the outbox events:
   ```sql
   SELECT processed_at FROM outbox_events
    WHERE event_type='customer.deleted' AND aggregate_id='<hash>';
   ```
   `processed_at` should be non-null within a few seconds.
3. **Verify cache eviction.** Hit the dashboard — the deleted customer
   shouldn't appear in any view. If it does, a stale snapshot is the
   likely cause; re-run the snapshot writer (`make snapshot`).
4. **Document the deletion.** Stash the response JSON + a screenshot of
   the audit-log entry in the customer's compliance folder. Some
   regulators require evidence of action within a fixed window.

## Things this DOES NOT do

- **Backups** — DR backups still contain the customer's data until the
  retention window passes. ADR 0008's backup tier sets that to 35 days.
  The customer's request triggers a regulatory clock; if the retention
  window is longer than the clock, the operator must additionally
  schedule a backup-rotation cycle.
- **Lakehouse historical files** — Iceberg snapshots are immutable.
  The deletion-ID journal lets the next training run filter; truly
  removing the rows requires a `REWRITE` operation (Iceberg native
  feature) on the affected partitions.
- **LLM-cached responses** — these are PII-redacted before caching, so
  the customer name shouldn't appear in cached prompts. If it does, the
  redactor missed a category — file as a Sev-2 against `src/pii.py`.
- **External processors** — your Sentry / Tempo / log retention systems
  hold their own copies. Run them through your standard processor-
  deletion procedure separately.

## When this runbook needs an update

- A new tenant joins under a stricter regime (HIPAA, FedRAMP) — review
  the steps for additional evidence requirements.
- A new derived store is added to ADR 0008's tier table — add a
  consumer that honours `customer.deleted` events.
- The deletion ID format changes — keep this runbook's example output
  aligned.

## Related

- ADR 0006 — auth (admin session needed to invoke this)
- ADR 0014 — split-plane architecture (the outbox + relayer this fans
  the deletion event through)
- `api/admin/gdpr.py` — implementation
- `tests/test_gdpr.py` — confirmation-required + audit-log invariant
- `deploy/dr-runbook.md` — backup rotation that complements this path
