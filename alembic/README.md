# Alembic — schema migrations

The application database (settings, audit log, outbox events) evolves through
Alembic migrations rather than `Base.metadata.create_all()`. Production
containers run `alembic upgrade head` either via the Dockerfile entrypoint
(`RUN_MIGRATIONS=auto`) or via the Helm pre-deploy Job
(`deploy/k8s/migrate-job.yaml`, used when `RUN_MIGRATIONS=skip`).

## Common operations

```bash
make migrate              # alembic upgrade head
make migrate-status       # alembic current + alembic history
alembic revision --autogenerate -m "add foo column"
alembic downgrade -1      # roll back one
```

## The expand/contract pattern (required for non-trivial changes)

The cloud-agnostic research blueprint
(`research/deep-research-report.md` §"Schema evolution and data migration")
mandates **compatibility-first expand/contract** for any change that affects
in-flight readers/writers — i.e., everything beyond a strict additive change
to a non-required column. The seven steps:

1. **Expand** — add the new column / index / table in a *backward-compatible*
   way: nullable, with a sensible default if the application reads it.
2. **Dual-write** — application writes both the old and the new shape.
   Old shape stays canonical for now.
3. **Backfill** — populate historical rows in *resumable, idempotent*
   chunks. Use a separate migration or a one-shot script; chunk size 1k–10k
   rows depending on lock-cost analysis.
4. **CDC keeps targets in sync** — log-based replication / outbox events
   keep downstream projections (search index, OLAP store, cache) coherent
   while the backfill is running.
5. **Shadow read** — application reads the new shape but compares to the
   old shape; metric/log any mismatch. Catch bugs before cutover.
6. **Cut over** — flip canonical to the new shape. Old shape still
   readable; new writes go to new shape only.
7. **Contract** — after a soak window (typically one week of production
   traffic), drop the old column / table.

Why seven steps and not "just add a NOT NULL column":
- Steps 1+3 mean a migration is *resumable* — a backfill that fails
  halfway can be re-run without corruption.
- Step 4 means downstream consumers (Iceberg lakehouse, ClickHouse,
  OpenSearch index) catch up automatically.
- Step 5 is the safety net — production traffic exposes the bugs that
  staging never will.
- Step 6 vs 7 separated by a soak window means rollback is just *flip
  the canonical flag back* — no data loss.

## Migration template

```python
"""<short description>.

<longer description, including which expand/contract step this is>.

Revision ID: <new>
Revises: <previous>
Create Date: YYYY-MM-DD
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "<new>"
down_revision: Union[str, None] = "<previous>"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # STEP 1 (expand): nullable column, sensible default if read by application.
    op.add_column(
        "<table>",
        sa.Column("<new_col>", sa.String(64), nullable=True),
    )
    # If you need an index, add it CONCURRENTLY in production
    # (Postgres-only; alembic doesn't wrap it). For Postgres-specific
    # index creation, use op.execute("CREATE INDEX CONCURRENTLY ...")
    # so writes aren't blocked.

    # ❌ DO NOT in this migration:
    #   - SET NOT NULL on the new column (do this in step 7's contract migration)
    #   - DROP the old column (also step 7)
    #   - Backfill via UPDATE on a large table (do this in a separate migration
    #     or one-shot script with batching)


def downgrade() -> None:
    op.drop_column("<table>", "<new_col>")
```

## What NOT to do in a single migration

- ❌ `ALTER TABLE ... ADD COLUMN x NOT NULL DEFAULT 'foo'` on a large
  table — Postgres rewrites the whole table. Use the seven-step pattern.
- ❌ `DROP COLUMN x` while in-flight code still reads from it. Contract
  step (7) only after a soak window with shadow reads green.
- ❌ Mixing schema changes with data backfill in the same migration —
  they have different failure / retry semantics. Split.
- ❌ Adding a unique constraint on a populated column without a check
  for existing duplicates first.
- ❌ Using `op.alter_column(..., type_=...)` to change a column's type
  on a large table without expand/contract — see the cloud-agnostic
  research blueprint, "Schema evolution" section.

## Existing migrations

| Revision | Adds | Notes |
|---|---|---|
| `0001` | `settings`, `audit_log` | Initial schema. Captured what `Base.metadata.create_all()` was doing on the fly; subsequent changes go through new migrations. |
| `0002` | `outbox_events` | Transactional outbox for CDC fan-out (ADR 0014). Indexes on `(processed_at, created_at)` for the relayer's "fetch unprocessed in order" scan and on `(aggregate_type, aggregate_id)` for entity-history reads. |

## Testing migrations

`tests/test_outbox.py` and `tests/test_admin.py` exercise the schema in
real SQLite for every test run; `make migrate` against a fresh Postgres
database is the production-equivalent check for pre-deploy. Add a CI
step that runs `alembic upgrade head` followed by `alembic downgrade
-1` followed by `alembic upgrade head` against a throwaway Postgres
container — round-trip catches half of the alembic mistakes that pure
forward-only deploys miss.

## Related

- `deploy/k8s/migrate-job.yaml` — Helm pre-install/upgrade Job
- `Dockerfile` entrypoint — `RUN_MIGRATIONS=auto|skip`
- ADR 0011 §"Alembic migrations"
- ADR 0014 §"Schema evolution: expand/contract"
- `research/deep-research-report.md` §"Schema evolution and data migration"
