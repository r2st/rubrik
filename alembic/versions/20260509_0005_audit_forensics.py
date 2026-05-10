"""Audit log: add ip_address + user_agent for forensic context.

A SOC-2 / ISO-27001 audit of the previous schema flagged that the
audit_log answers "who/what/when" but not "from where." Adding two
nullable columns closes that gap without breaking historical rows.
The admin routes populate them best-effort from the request; CLI
mutations (alembic-driven seed, fixture setup) leave them NULL.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column("ip_address", sa.String(64), nullable=True),
    )
    op.add_column(
        "audit_log",
        sa.Column("user_agent", sa.String(256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_log", "user_agent")
    op.drop_column("audit_log", "ip_address")
