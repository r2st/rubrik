"""Add trace_id column to outbox_events for distributed-tracing propagation.

Step 1 of expand/contract — nullable column, application reads it
defensively. No backfill required (historical rows simply have NULL,
which the consumer treats as "no parent span").

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-07
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("trace_id", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "trace_id")
