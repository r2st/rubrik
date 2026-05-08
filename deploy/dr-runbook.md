# Disaster-recovery runbook

ADR 0014 §"Backup, PITR, DR" adopts the cloud-agnostic research's defaults
for backup, point-in-time recovery, and regional failover. This document
makes those defaults concrete: who restores what, when, and how to know it
worked.

## Posture

| Dimension | Default |
|---|---|
| Primary write region | Multi-AZ synchronous replication |
| Warm DR region | Async replication or streaming projection |
| RPO (data loss tolerance) | **≤ 5 minutes** |
| RTO (time to recovery) | **≤ 30 minutes in-region**, **≤ 2 hours cross-region** |
| Backup retention | **35 days** for OLTP base + WAL; indefinite for Iceberg lakehouse (object-storage cost-optimised) |
| Restore-drill cadence | **Monthly** restore from backup; **quarterly** cross-region failover |
| Backup immutability | **Object-lock + versioning** on the object-store bucket; no IAM principal can delete unversioned |

## What gets backed up

| Tier | What | How | Where |
|---|---|---|---|
| OLTP (Postgres) | Continuous WAL + nightly base backups | Cloud-managed RDS / Cloud SQL / Aurora; or pgBackRest if self-managed | Object storage in DR region with object-lock |
| Outbox table | Same as OLTP — it's a Postgres table | (covered by the OLTP backup) | (same) |
| Admin DB (settings + audit_log) | Same as OLTP | (same) | (same) |
| Hot analytics (ClickHouse) | Daily Parquet export to object storage | `clickhouse-backup` or managed equivalent | Object storage; cross-region replicated |
| Lakehouse (Iceberg on S3) | Versioned by design (Iceberg snapshots) | No additional backup needed; replicate the bucket | Cross-region replication |
| Redis | Not backed up — treated as a disposable accelerator | (none) | (none — re-warmed from origin) |
| LoRA adapters (S3) | Versioned bucket; tagged by training run | Train job writes versioned objects | Object storage; cross-region replicated |
| Snapshot of `PipelineState` | Versioned by checksum; the CronJob rewrites every 5 min | Object storage + manifest | Same bucket as lakehouse |
| Secrets (bootstrap.toml in K8s Secret) | Backed up by the secret manager (KMS-encrypted) | Provider-native (AWS Secrets Manager, GCP Secret Manager, Azure Key Vault) | Provider region; replicate as needed |

## PITR — Postgres

### Trigger conditions
- Logical corruption (bad migration, unintended UPDATE without WHERE, application bug)
- Operator error (`DROP TABLE` in the wrong env)
- Compliance request (point-in-time evidence)

### Procedure
1. **Freeze writes** — flip the application to read-only via the admin panel (`feature.read_only_mode` runtime setting; the `/api/v1/admin/*` writes return 503 with `Retry-After`).
2. **Identify the timestamp** to restore to. Read the audit log + Postgres logs to bracket the bad change.
3. **Spin up a parallel cluster** from the base backup that precedes the timestamp.
4. **Replay WAL** to the chosen LSN / timestamp.
5. **Validate** — query a known-good row that should be present at that timestamp.
6. **Cut over** — flip the application's `database.url` in `bootstrap.toml` to the restored cluster.
7. **Unfreeze writes**.
8. **Postmortem** — what got us here, what would have caught it sooner. Update tests + runbooks.

### Drill cadence
Monthly. The drill restores into a `dr-drill-<date>` namespace, runs the validation step against a synthetic dataset, and tears the cluster down. Failure of two consecutive drills escalates to Sev-2 — the backup itself is suspect.

## Cross-region failover

### Trigger conditions
- Primary region availability outage > 30 min with no estimated recovery
- Cloud-provider control-plane failure that prevents new resource creation in primary
- Compliance event requiring traffic to leave the primary region

### Procedure
1. **Declare DR mode** in the incident channel; assign an Incident Commander.
2. **Promote the warm replica** in the DR region to primary. (Cloud-managed: one console action; self-managed: pgctl promote.)
3. **Update DNS / Gateway API HTTPRoute** so the public name now points at DR-region ingress. TTLs on the public records are kept short (60 s) for this reason.
4. **Re-point the K8s `bootstrap-secret`** at the DR-region database URL (via the secret-manager replication entry).
5. **Roll the API and worker Deployments** in the DR region — readiness probes will gate traffic.
6. **Verify** — `/api/ready` returns 200, `/api/live` returns alive, and a synthetic end-to-end probe (`make smoke-test`) passes.
7. **Drain Kafka backlog** in the DR region if the primary's Kafka was unreachable.
8. **Communicate** — status page, customer success, internal stakeholders.

### Drill cadence
Quarterly. Drill mode is identical to a real failover except DNS isn't actually flipped — the synthetic probes hit the DR region directly. A successful drill produces a report logged in `docs/dr-drills/`.

## Recovery validation

After **every** recovery (drill or real), the following must be re-verified:

- [ ] All Postgres tables present, last row's `created_at` ≤ the chosen restore timestamp
- [ ] Outbox table empty or with all rows `processed_at IS NOT NULL`
- [ ] Admin login works (PBKDF2 hash verifies; session cookie issues)
- [ ] Snapshot manifest is fresh; replicas warm from it
- [ ] Synthetic load test reaches the regression baseline RPS within 30%

If any of those fails, the recovery is incomplete — escalate.

## Things we deliberately don't do

- **No active-active multi-region writes.** Adds coordination complexity and consistency caveats that aren't justified by current SLOs. ADR 0014's "When to revisit" lists the trigger.
- **No zero-RPO commitment.** Synchronous cross-region replication doubles write latency; we'd rather have 5-min RPO at single-region p95 latency than 0 RPO at 2× p95.
- **No "just restore from yesterday's backup."** PITR is the backup; nightly snapshots alone leave a 24-hour RPO that violates the SLO.
