# Security Policy

## Reporting a Vulnerability

If you believe you have found a security vulnerability in this project,
please report it privately so we can fix it before public disclosure.

**Contact:** sumaninster7@gmail.com

Please include:
- A description of the issue and its potential impact.
- Steps to reproduce (proof-of-concept code, request payloads, etc.).
- Affected version / commit SHA.
- Your suggested mitigation if you have one.

We aim to:
- Acknowledge receipt within **2 business days**.
- Triage and confirm the issue within **5 business days**.
- Ship a fix or mitigation within **30 days** of confirmation, depending on
  severity.

Please do **not** open public GitHub issues, pull requests, or social-media
posts about a suspected vulnerability before it has been remediated.

## Scope

In scope:
- The FastAPI service in `api/`.
- The pipeline and data-handling code in `src/`.
- The Alembic migrations and DB schema.
- Build / deploy artifacts under `Dockerfile`, `docker-compose.yml`, `deploy/`.

Out of scope:
- Vulnerabilities in third-party dependencies (please report upstream — we
  track CVEs via `pip-audit` in CI).
- Findings that require physical access, a compromised admin account, or
  social engineering of the operator.
- Denial-of-service via volumetric traffic against an unprotected origin
  (deploy behind a CDN / WAF in production).

## Hardening Already in Place

For context, the service implements:

**Authentication**
- PBKDF2-SHA256 (200k iterations) for the admin password.
- HMAC-signed session cookies with `Secure` (prod) + `HttpOnly` + `SameSite=Strict`.
- Session secret externalizable via `auth.session_secret_path` (file-mounted in K8s).
- **TOTP MFA** for the admin account (authenticator app via `pyotp`).
  Secret stored as the masked `secret` runtime type; never round-trips
  through the UI in plaintext after setup.
- **Single-use backup codes** — 8 `XXXX-XXXX` codes minted at TOTP
  setup, SHA-256 hashed at rest, surfaced once to the operator.
  Substitute for a TOTP code on `/admin/login`; each consumed atomically.
  Recovery without shell access. `POST /admin/totp/backup-codes/regenerate`
  rotates the batch.
- **JWT auth** (opt-in) — HS256 / RS256 / ES256 via JWKS, with `aud`/`iss`
  claim checks.
- **Per-tenant data isolation** — `src/tenant.py` ContextVar derived
  from JWT claims (`tid`/`tenant`/`tenant_id`) or hashed `X-API-Key`.
  `DatabaseRepository.get(meeting_id)` automatically scopes by tenant
  when the ContextVar is set; the cached layer keys entries by
  `{tenant}:{meeting_id}` so cross-tenant cache leaks are impossible.

**Request hardening**
- 1 MiB body cap to prevent buffer-exhaustion DoS.
- **CSRF** — double-submit cookie + `Sec-Fetch-Site` check on every admin
  write route.
- Three-layer rate limiting: per-tenant slowapi (Redis-backed when
  configured) + concurrency cap (`BackpressureMiddleware`) + adaptive
  throttle that sheds when p95 breaches SLO.
- Strict 5/min/IP cap on `/admin/login` + `/admin/password`.
- 60/min/IP cap on every other admin write endpoint.
- **Idempotency-Key** middleware makes POST retries safe (replay-cached
  responses, 409 on hash conflict).

**Headers + responses**
- CSP, HSTS (prod), X-Frame-Options, X-Content-Type-Options,
  Referrer-Policy, Permissions-Policy.
- CORS — default `auth.cors_origins` is `[]` (forces explicit allowlist).
  `_safe_cors_origins()` strips `"*"` in prod and logs a warning when
  combined with `allow_credentials=True`.
- `/robots.txt` blocks crawlers from `/admin` + `/api/v1/admin`.
- `/.well-known/security.txt` (RFC 9116) points researchers at this
  document.
- Standardized error envelope — no framework internals leak.

**Data privacy**
- **PII redaction** at egress (LLM gateway, outbox events) — emails,
  phones, SSN, Luhn-validated credit cards, IPs, common API-key shapes.
- **PII scrubbing in logs** — `_PiiScrubFilter` runs every log record
  through the redactor before any formatter sees it.
- **GDPR right-to-be-forgotten** — `POST /admin/gdpr/delete-customer`
  performs an atomic delete + outbox event + hashed audit row + cache
  invalidation. The DELETE is a structured JSONB match on
  `raw->'info'->>'customer'` (Postgres) / `json_extract` (SQLite) —
  never a free-text LIKE, so a substring collision can't delete
  unrelated rows. Audit row never carries the raw customer name. See
  `deploy/gdpr-runbook.md`.
- **Bulk export quotas** — `GET /admin/meetings/export.ndjson` is
  gated by both the 60/min/IP admin-write rate limit AND a per-actor
  per-UTC-day cap (`export.max_per_day`, default 4). A stolen session
  cannot exfiltrate the dataset in a loop.

**Operational integrity**
- Audit log for every admin mutation (settings, password rotation,
  TOTP setup/disable, GDPR deletion, outbox replay/reap, snapshot
  rebuild). `secret`-typed values are masked in audit rows so the raw
  key is not recoverable from history. Each row also records
  `ip_address` + `user_agent` for forensic context (migration 0005).
- **Snapshot HMAC integrity** — `admin.snapshot_signing_secret`
  (bootstrap-only, file-mounted) HMAC-SHA256-signs the snapshot
  manifest at write. Read refuses any unsigned or signature-mismatched
  manifest once a key is configured — closes the pickle-RCE primitive
  that an unsigned S3 snapshot otherwise creates. The signing key is
  *deliberately* not a runtime setting: a compromised admin must not
  be able to disable signing.
- **Outbox lifecycle** — a background reaper deletes processed
  `outbox_events` rows older than `outbox.reap_processed_days`
  (default 7d). Plus `POST /admin/outbox/reap` operator override.
  Trace IDs from `emit()` propagate into Kafka `traceparent` headers
  so producer → relayer → consumer is one trace.
- Migration round-trip CI gate (`alembic upgrade → downgrade -1 →
  upgrade`) blocks merges that break alembic semantics.
- Sev-1 runbook: `deploy/sev1-runbook.md`. Smoke-test:
  `make smoke-test` against any live deployment.
- DR posture: PITR + 35-day backups + monthly restore drill +
  quarterly cross-region failover (`deploy/dr-runbook.md`).

If your report touches one of these areas, please mention which control you
believe is bypassed.
