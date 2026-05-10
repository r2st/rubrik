"""Create the canonical meetings table the DatabaseRepository targets.

Originally we punted on this — ADR 0008 framed ``meetings`` as a
"data-platform-owned" table outside the application's alembic scope.
In practice, every deployment that flips ``transcripts.repository`` to
``database`` first ran ``DatabaseRepository.import_from_local`` and hit
"relation does not exist" because nobody owned the DDL. Owning it from
alembic closes that gap and gives the GDPR delete path a stable target.

The schema is small on purpose: ``meeting_id`` (PK) + ``raw`` JSONB +
``created_at``. JSONB lets the analytical tier evolve without ALTERs.
``tenant_id`` is added nullable here so the per-tenant filter (added in
the same change set) can use it; existing rows backfill to the legacy
single-tenant value via the application config.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # JSONB on Postgres for indexability; plain JSON on SQLite (dev only).
    raw_type = postgresql.JSONB(astext_type=sa.Text()) if is_pg else sa.JSON()

    op.create_table(
        "meetings",
        sa.Column("meeting_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("raw", raw_type, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
    )
    op.create_index(
        "ix_meetings_tenant_id", "meetings", ["tenant_id"],
    )
    op.create_index(
        "ix_meetings_created_at", "meetings", ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_meetings_created_at", table_name="meetings")
    op.drop_index("ix_meetings_tenant_id", table_name="meetings")
    op.drop_table("meetings")
