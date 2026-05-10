# ADR 0009: Admin Panel for Runtime Config — No Env Variables for App Config

- **Status:** Accepted
- **Date:** 2026-05-06
- **Supersedes:** the env-var-based settings model from ADR 0006 era (twelve-factor-style `LOG_LEVEL`, `API_KEY`, `RATE_LIMIT_DEFAULT`, etc.)

## Context

The system had grown a meaningful number of operationally-tunable knobs:

- **Auth & access:** API key, CORS origins
- **Rate limiting:** default + strict limits
- **Pipeline lifecycle:** refresh interval
- **Risk scoring:** three weights + two thresholds (the things a CS team most wants to tune)
- **Sentiment:** within-meeting friction threshold
- **Feature flags:** metrics enabled, tracing enabled, sentry sampling rate

All of these were env vars (`API_KEY`, `RATE_LIMIT_DEFAULT`, etc.) loaded by pydantic-settings. That pattern has well-known limits:

| Problem | Why env vars hit it |
|---|---|
| **Restart required** to change anything | Env vars only read at process start |
| **No audit trail** | Who changed `RATE_LIMIT_DEFAULT` to `10/minute` last Tuesday? Lost. |
| **Operator UX** | Editing kubernetes secrets / docker-compose envs to tune `risk.threshold_high` is friction that prevents the iteration the score deserves |
| **Drift between environments** | Each environment's env block diverges; reconciling is painful |
| **No type validation** at edit time — typos in env values surface as runtime errors |

The CS team should be able to **slide the churn-risk threshold** without filing a ticket and waiting for a deploy.

## Decision

We **eliminate environment variables for application configuration** entirely. Two complementary layers replace them:

### 1. Bootstrap config in `bootstrap.toml`

The minimum needed to start the service before the DB is reachable:
- `[app]` env, log level, log format
- `[database]` URL
- `[admin]` initial password + session secret
- `[paths]` dataset path override
- `[observability]` Sentry / OTel endpoints (these reinit on change anyway)

`bootstrap.toml` is gitignored; `bootstrap.toml.example` is the committed template. Operators copy and edit. No env vars consulted by the application — `os.environ` reads are restricted to runtime infrastructure (uvicorn, kubernetes liveness probes, etc.).

### 2. Runtime config in the admin DB, edited via `/admin`

Everything else lives in the `settings` table:

```
key                                  value           type   category
auth.api_key                         ""              str    auth
auth.cors_origins                    ["*"]           list   auth
rate_limit.default                   "120/minute"    str    rate_limit
rate_limit.strict                    "30/minute"     str    rate_limit
rate_limit.per_tenant                {}              json   rate_limit
pipeline.refresh_minutes             0               int    pipeline
risk.weight_low_sentiment            0.5             float  risk
risk.weight_churn_signals            0.3             float  risk
risk.weight_negative_pivots          0.2             float  risk
risk.threshold_high                  0.40            float  risk
risk.threshold_medium                0.25            float  risk
sentiment.negative_pivot_threshold   -0.5            float  sentiment
feature.metrics_enabled              true            bool   feature
feature.observability_traces         true            bool   feature
observability.sentry_traces_sample_rate
                                     0.1             float  observability
observability.otel_sample_rate       0.01            float  observability
backpressure.max_inflight            128             int    backpressure
snapshot.url                         ""              str    snapshot
snapshot.poll_seconds                30              int    snapshot
distribution.redis_url               ""              str    distribution
llm.tier1_endpoint                   ""              str    llm
llm.tier2_enabled                    false           bool   llm
llm.tier2_provider                   "anthropic"     str    llm
llm.tier2_model                      "claude-sonnet-4-5"  str llm
llm.tier2_api_key                    ""              secret llm
llm.tier2_daily_budget_usd           50.0            float  llm
llm.tier2_request_timeout_s          30              int    llm
auth.jwt_enabled                     false           bool   auth
auth.jwt_algorithm                   "HS256"         str    auth
auth.jwt_secret                      ""              secret auth
auth.jwt_jwks_url                    ""              str    auth
auth.jwt_audience                    ""              str    auth
auth.jwt_issuer                      ""              str    auth
auth.csrf_enabled                    true            bool   auth
auth.admin_totp_secret               ""              secret auth
auth.admin_totp_required             false           bool   auth
observability.pii_scrub_logs         true            bool   observability
idempotency.enabled                  false           bool   idempotency
idempotency.ttl_hours                24              int    idempotency
idempotency.max_body_bytes           16384           int    idempotency
transcripts.repository               "local"         str    transcripts
```

Reads go through a **5-second TTL cache**. On Postgres, every `set()` additionally publishes a `settings_changed` `NOTIFY`, and a listener thread on each replica drops the cache locally — operator changes propagate in < 100 ms instead of waiting for the TTL. SQLite (dev) silently skips the publish; the TTL is the safety net.

#### The `secret` type

API keys (and any future credential-shaped value) are typed `secret`. The runtime store keeps the **raw value** so the consuming application code can use it; every API read path runs the value through ``mask_secret()`` first, which renders empty as `""` and otherwise as `"••••••<last 4>"`. The audit log writes the masked form for both `old_value` and `new_value` on `secret`-typed rotations — the historical record can't leak the key. The admin UI renders `secret` as a `<input type="password">` with the masked placeholder; an empty submission preserves the existing key (no accidental wipes), a non-empty submission rotates it. See `tests/test_secret_settings.py` for the expected behaviour, including the audit-log invariant.

### Admin panel architecture

```mermaid
flowchart LR
    Browser[Operator's browser] -->|password| Login[POST /api/v1/admin/login]
    Login -->|verify scrypt hash| DB[(Settings DB)]
    Login -->|signed cookie| Browser

    Browser -->|cookie| UI[GET /admin · static HTML]
    Browser -->|XHR| API[/api/v1/admin/*]
    API -->|require_admin dep| AuthCheck{Valid<br/>session?}
    AuthCheck -->|yes| Routes[settings · audit · password]
    AuthCheck -->|no| Reject[401 + envelope]

    Routes <--> DB
    Routes -->|every change| Audit[(audit_log)]
```

- **Auth:** PBKDF2-SHA256 password hash (200k iterations, OWASP 2023). Stateless signed session cookies (HMAC-SHA256 over `session_secret`). Initial password from bootstrap; rotated through the panel.
- **Routes:** `/api/v1/admin/{login, logout, me, settings, audit, password}` — standard REST. Single admin role for now.
- **UI:** Vanilla JS / no build step. Same look-and-feel as the public dashboard.

## Consequences

**Positive**
- Operators tune live without deploys — the iteration loop on risk scoring shrinks from days to seconds
- Every change is recorded with actor + timestamp + old/new values; "why did the rate limit change?" has an answer
- Twelve-factor compatibility is preserved at the *bootstrap* layer (a single file path, not 30 env vars)
- The pattern scales to multi-tenancy: when we add tenants, settings get a `tenant_id` column and the admin panel grows a tenant selector

**Negative**
- One more dependency: SQLite (or Postgres) is now required to *start* the service
- Operators need to learn the admin panel — not difficult, but new
- Cache TTL of 5s means a settings change can take up to 5 seconds to propagate across all in-flight requests; fine for our use cases, would be tighter at very high throughput

**Neutral**
- The admin DB is small (~20 settings rows + audit log). Even at 100M scale (ADR 0008), this table stays tiny. It lives alongside the analytical stores, not as a bottleneck.

## What we deliberately did not do

| Choice | Why |
|---|---|
| **Per-tenant configuration** | Single-tenant for now (ADR 0006). When multi-tenancy arrives, add a `tenant_id` column to `settings` + a tenant filter on every read. |
| **Roles / permissions** | Single admin role suffices. RBAC can layer on top via additional `admin_users` table + scope claims in the session. |
| **Settings versioning + rollback** | Audit log gives the *history* but not one-click revert. A future feature once it earns its keep. |
| **Hot-reload without TTL** | DB write triggers / pubsub for instant propagation. 5s TTL is plenty given the use case. |
| **External IDP (OAuth2/OIDC)** | Out of scope for single-admin model. ADR 0006 documents the migration path. |

## When to revisit

- Multi-tenancy lands → add tenant filters + tenant-aware admin panel
- Operator base grows beyond ~5 people → roles + audit-log filters
- Settings change frequency exceeds ~100/day → revisit the TTL strategy or move to event-driven invalidation
- A managed feature-flag SaaS becomes worth the dependency (LaunchDarkly, Statsig) → migrate the `feature.*` namespace

## Related

- ADR 0005 — "no database" — partially superseded by ADR 0008 (which adds the operational DB)
- ADR 0006 — API key auth — the api_key value moves into the runtime store managed here
- `src/settings.py` — bootstrap loader
- `src/runtime_settings.py` — DB-backed runtime store
- `api/admin/` — admin panel API + auth
- `web/admin.html` — admin UI
- `tests/test_admin.py` — 16 end-to-end tests for the admin flow
