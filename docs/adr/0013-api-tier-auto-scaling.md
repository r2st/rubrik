# ADR 0013: API Tier Auto-Scaling — Closing the Gap with the ML Pipeline

- **Status:** Accepted (initial implementation landed; deploy artifacts target k8s rollout)
- **Date:** 2026-05-07

## Context

[ADR 0010](0010-auto-scaling-ml-pipeline.md) settles the ML pipeline's auto-scaling story (Ray Data ETL, multi-node FSDP training, autoscaled vLLM serving with KubeRay + Karpenter). The **API tier** — the FastAPI service that fronts everything — has its own scaling concerns that ADR 0010 didn't cover.

The current implementation already gets several things right:
- Stateless cookie auth (no sticky sessions needed)
- Connection pool tuning + pre-ping (`src/db.py`)
- Lifespan-based startup/shutdown
- ETag + Cache-Control on read endpoints
- Body-size cap + per-IP rate limit (slowapi)
- Repository pattern (so the data source is swappable per ADR 0011)

But several gaps will bite under real production load. Each is small individually; together they make the difference between "scales gracefully to 50 replicas" and "thunders into cascading failure at 10."

## Decision

Adopt the 15 improvements below as the API tier's auto-scaling target. Ship them incrementally — none are blocking on each other except where noted.

### 1. Shared `PipelineState` snapshot (cold-start fix)

**Problem.** Every replica calls `state._build()` on first request → full pipeline rebuild. At 100M records this is minutes per pod. HPA scale-up is a guaranteed latency cliff.

**Fix.** A singleton job (see #2) writes the analytical state to **S3 (Parquet) + Redis (small dataframes)** with a manifest pointer. New replicas read the snapshot — pipeline cache warms in seconds, not minutes.

```text
api/state.py:
  _build() →
    if SNAPSHOT_URL set:
        return _load_snapshot(SNAPSHOT_URL)   # < 5s typical
    return _build_from_repository()           # local fallback
```

Snapshot format: a versioned directory `s3://bucket/snapshots/{ts}/{shard}.parquet` + `manifest.json` with the dataset hash. Replicas poll the manifest's mtime; bump → reload.

### 2. Externalized refresh (one writer, many readers)

**Problem.** Each replica runs its own `asyncio` refresh task. N replicas × full rebuild = N× compute, N× DB pressure, drift between replicas.

**Fix.** Move refresh to a **k8s CronJob** that runs the pipeline once per interval, writes the snapshot in #1, and bumps the manifest. Replicas read-only. Keep the in-process refresh task as a fallback (single-replica dev, where there's no Cron).

Exact-once semantics aren't needed; the job is idempotent.

### 3. Cluster-wide rate limiting (Redis-backed)

**Problem.** slowapi keeps state in-process. An attacker hitting 10 replicas gets 10× the limit. The strict admin-login limiter has the same problem (see `_strict_window` in `api/admin/routes.py`). Multi-tenant fairness is impossible.

**Fix.** Switch to **`slowapi[redis]`** keyed on `(tenant_id, ip)`. Move the strict admin limiter to the same Redis. Per-tenant caps live in `runtime_settings` so the admin UI can tune them.

Falls back to in-process if Redis unavailable (degraded but functional).

### 4. HPA on custom metrics (RPS + p95 latency)

**Problem.** CPU-based HPA underscales I/O-bound FastAPI. CPU stays low while queues build → undersized under load, oversized at idle.

**Fix.** Expose request-rate, p95 latency, and in-flight-request gauge via Prometheus. Wire **Prometheus Adapter** so HPA can target `http_request_p95_seconds < 0.2` and `requests_per_second_per_replica < 200`. Keep CPU as a guardrail floor.

### 5. Migrations as a Kubernetes Job (not container start)

**Problem.** `alembic upgrade head` runs on every container start. Under rolling deploy with N replicas, all race for the alembic lock. Slow migrations stretch deploys; failed migrations take down the rollout.

**Fix.** Helm chart **pre-install / pre-upgrade Job** runs migrations to completion. Rolling update only proceeds on Job success. Container entrypoint becomes a passthrough (no `alembic upgrade head` on boot). Keep the entrypoint variant for `docker compose` and dev.

### 6. PgBouncer in front of Postgres

**Problem.** `pool_size=5` + `max_overflow=10` × 50 replicas = 750 connections. Postgres tops out (default 100, RDS sized by tier). Replicas thrash on connection backoff.

**Fix.** Deploy **PgBouncer in transaction mode**; replicas connect to PgBouncer with `pool_size=2`. Cluster-wide connection budget stays bounded regardless of replica count. Disable SQLAlchemy's `pool_pre_ping` when behind PgBouncer (it kills the multiplexing benefit).

### 7. Concurrency cap + 503 backpressure

**Problem.** uvicorn queues requests forever. Slow downstream → request pile-up → OOM → cascade.

**Fix.** Per-process **asyncio.Semaphore** cap on inflight requests (~`workers × 2 × cpu_count`). Over the cap → return `503 + Retry-After: <jittered>`. The LB sheds load instead of escalating it.

Implement as a middleware: `BackpressureMiddleware`.

### 8. Circuit breakers on every external call

**Problem.** Slow DB / vLLM / frontier-LLM call → request pile-up → OOM. Goes well past timeouts.

**Fix.** Wrap external calls in **`pybreaker`** (sync) or **`purgatory`** (async). On open circuit, return last-good cached response with `X-Stale-Response: true` (we already have the header). Targets:
- DB calls in `runtime_settings.get()` (cache TTL is the natural fallback)
- vLLM gateway (per ADR 0010)
- Frontier-LLM gateway (per ADR 0012)

### 9. Settings-change push invalidation

**Problem.** `runtime_settings` cache TTL is 5s. Operator changes propagate eventually; replicas can lag by up to 5s.

**Fix.** **Postgres `LISTEN/NOTIFY`** on the settings table → `runtime_settings._invalidate()` immediately. Falls back to TTL if NOTIFY connection drops. Redis pub/sub equivalent if Redis is already in the stack (#3).

This is small but materially improves operator-feedback feel for incident response.

### 10. Split liveness vs readiness probes

**Problem.** `/api/health` returns 200 unconditionally (process up). k8s sends traffic to replicas before pipeline cache is warm → first requests time out.

**Fix.**
- `GET /api/live` — process up, event loop responsive (no DB check)
- `GET /api/ready` — `state.is_warm() and db.healthy()` — k8s readiness probe gates traffic on this

A readiness flap during refresh (rare) drains the replica briefly; that's correct behavior.

### 11. Background job queue for off-path work

**Problem.** ETL, batch validation, and (eventually) training-data prep run on the API process. They share the event loop with user requests.

**Fix.** Deploy **Arq** (async-native, lightweight) as a separate Deployment. Replicas enqueue jobs; workers consume. Worker count scales by queue depth via a Prometheus metric. Keep the synchronous CLI paths (`make run`) for dev.

### 12. CDN at the edge

**Problem.** Static frontend + GET-cacheable APIs hit the origin every time.

**Fix.** **CloudFront / Fastly** in front. Routes already emit ETag and `Cache-Control: max-age=60` so the work is mostly Helm + the right `private` vs `public` directives per route. Admin routes and POST/PUT bypass the CDN.

Edge cache hit rate becomes a headline ops metric.

### 13. Per-tenant fairness

**Problem.** One noisy tenant can starve the global rate budget.

**Fix.** Token bucket per tenant in the same Redis (#3). Defaults in `runtime_settings`; admin can override per tenant. Headers: `X-RateLimit-Tenant-*` so consumers see their own limits.

Requires identity (tenant ID per request), which today comes from the API key but ADR 0006 documents the JWT migration path. This unlocks once that lands.

### 14. OTel Collector + tail-based sampling

**Problem.** Prometheus scraping every pod scales linearly with replica count; full-fidelity tracing at 1k RPS is wasteful.

**Fix.** **OTel Collector as a DaemonSet** receives traces/metrics from every pod via OTLP, applies tail-based sampling (1% baseline + 100% errors + 100% on slow tails), and pushes to the backend. Push gateway for batch jobs.

### 15. Graceful shutdown — explicit drain

**Problem.** SIGTERM today: lifespan stops the refresh task. But uvicorn keeps accepting new requests during the LB's drain window, in-flight DB calls may not drain, and audit-log writes can race.

**Fix.** Lifespan shutdown sequence:
1. Flip readiness to `false` (LB stops sending new traffic).
2. Sleep `terminationGracePeriodSeconds - 5` to let LB programs propagate.
3. Stop accepting new requests at the app layer.
4. `await asyncio.gather(*inflight, timeout=15)`.
5. Stop refresh task.
6. Flush audit-log buffer.
7. Close DB pool.

## Consequences

**Positive**
- API tier now scales like the ML tier: HPA on real signals, predictable cost, graceful degradation under partial failure.
- Cold-start latency drops from minutes to seconds.
- Cluster-wide enforcement of rate limits and connection budgets — no more per-replica leakage.
- Operator changes feel real-time.

**Negative**
- Operational footprint grows: Redis, PgBouncer, OTel Collector, Arq workers — each one a new thing to monitor.
- Snapshot format becomes a versioned contract; schema drift between writer (CronJob) and reader (replica) is a new failure mode.
- Per-tenant fairness depends on identity, which depends on auth migration (ADR 0006). Some items are gated.

**Neutral**
- The current single-replica dev workflow keeps working — every change has a degraded fallback (in-process refresh, in-memory rate limit, no CDN, no Redis) so `docker compose up` still gives a useful local stack.

## Sequencing

| Phase | Items | Unblocks |
|---|---|---|
| **A — Foundations** (≈ 1 week) | #5, #7, #10, #15 | Safe rolling deploys, backpressure, real probes — quick wins. |
| **B — Shared state** (≈ 2 weeks) | #1, #2, #6 | Cold-start fix, cluster-wide DB budget. |
| **C — Distribution** (≈ 1 week) | #3, #9, #14 | Redis becomes the cluster-wide signal layer. |
| **D — Adaptive scaling** (≈ 1 week) | #4, #8, #11, #12 | HPA on real metrics, circuit breakers, off-path work, edge caching. |
| **E — Multi-tenant** (≈ ongoing) | #13 | Per-tenant fairness — gated on auth migration. |

## When to revisit

- A failure mode lands that isn't covered (e.g., a replica deadlock that liveness misses but readiness should catch — refine probes).
- Cost telemetry shows the snapshot path is dominating storage spend → switch from full-state snapshots to delta encoding.
- Redis becomes the bottleneck → shard by tenant or move per-tenant state to per-tenant Redis instances.

## Related

- ADR 0008 — Data layer for 100M+ records (the source side of #1, #2)
- ADR 0009 — Admin panel + runtime config (where #3, #9, #13 expose tunables)
- ADR 0010 — Auto-scaling ML pipeline (this ADR is the API-side counterpart)
- ADR 0011 — Repository pattern + streaming (#1's `repository.all()` is replaced by snapshot read)
- `api/main.py`, `api/state.py`, `api/middleware.py` — current code touched by these changes
