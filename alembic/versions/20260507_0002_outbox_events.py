"""Add outbox_events table — transactional outbox for CDC fan-out.

ADR 0014 §"Hot path" introduces the outbox pattern so committed-write events
can be published to Kafka / Kinesis / Event Hubs without dual-writes from
the application layer.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-07
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False, server_default="0"),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_attempts", sa.Integer, nullable=False,
                  server_default="0"),
    )
    # Composite index on (processed_at, created_at) so the relayer's
    # "fetch unprocessed in order" scan is index-only.
    op.create_index(
        "ix_outbox_unprocessed",
        "outbox_events",
        ["processed_at", "created_at"],
    )
    # Lookup index for "all events for this aggregate" reads (e.g. consumer
    # replays a single entity's history).
    op.create_index(
        "ix_outbox_aggregate",
        "outbox_events",
        ["aggregate_type", "aggregate_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_aggregate", table_name="outbox_events")
    op.drop_index("ix_outbox_unprocessed", table_name="outbox_events")
    op.drop_table("outbox_events")
