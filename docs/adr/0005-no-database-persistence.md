# ADR 0005: No Database Persistence (Yet)

- **Status:** Accepted
- **Date:** 2026-04-20

## Context

A standard reflex when building a "production" service is to add a database. Postgres + SQLAlchemy + alembic is the path of least resistance. But before adding infrastructure we should ask: *what does it buy us?*

For this pipeline today:
- **Inputs are static JSON files** in `dataset/` — one read pass at startup
- **Outputs are derived state** (DataFrames, dicts) cached in a thread-safe singleton
- **There are no write operations** — every endpoint is `GET`
- **There is no per-user state** — single-tenant for now (see ADR 0006)
- **Pipeline rebuild takes ~10 seconds** — cheap to redo

A database would add:
- A migration framework, schema, and DDL/DML to maintain
- An ORM (or raw queries) and connection pooling
- A new failure mode (DB unavailable, slow queries, lock contention)
- Operational overhead (backups, monitoring, upgrades, DR)

## Decision

**No database. Pipeline state is held in-process, rebuilt at startup and on a configurable schedule.**

- `api/state.py` holds the singleton via `lru_cache`-style pattern with a thread lock
- Optional periodic refresh via `PIPELINE_REFRESH_MINUTES` setting — async task rebuilds the cache; no cron, no DB
- Outputs that need durability go through `output/` (CSVs, JSON, PNGs) written by `run_analysis.py`

## Consequences

**Positive**
- One fewer service to deploy, monitor, back up
- No schema migrations to coordinate during deploys
- Fast cold start: pipeline ready in ~10s on commodity hardware
- The singleton is trivially testable — pure Python objects in memory

**Negative**
- **Single instance only.** Behind a load balancer with N workers, each has its own copy of the cache. Acceptable today (the data is read-only and identical across instances) but not at scale.
- **No history.** We see a snapshot of the analysis, not how it changed over time. A real product would want to query "how did sentiment for customer X trend across the last 12 months?"
- **No multi-tenancy.** All data is assumed to belong to the same logical tenant.

## When to revisit

- We add **write operations** — annotations, user-flagged meetings, manual category overrides
- We add **multi-tenancy** — per-tenant data isolation, per-user preferences
- We need **historical queries** across time ranges that exceed a single pipeline build
- We go **multi-instance** and the per-instance memory cost becomes prohibitive
- The dataset size grows past memory (10k+ meetings with full transcripts)

When that happens, the natural target is **Postgres + SQLAlchemy + alembic**, with a Redis cache layer for hot reads. The split would be: source-of-truth data in Postgres, derived analysis cached in Redis.

## Related

- `api/state.py` — current singleton implementation
- ADR 0006 — single API key auth assumes single-tenant; databases earn their place when multi-tenant
