"""ORM models for the application database.

Two tables today:
  - `settings`   : key/value runtime config, edited via the admin panel
  - `audit_log`  : append-only record of every settings change

Both are intentionally tiny. At 100M+ scale these stay tiny (admin metadata),
while the analytical data lives in separate stores — see ADR-0008.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Setting(Base):
    """A single runtime configuration value.

    `value` is stored as JSON so we can hold strings, ints, floats, bools,
    and lists (e.g. `cors_origins`) in one column without a polymorphic schema.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[object] = mapped_column(JSON, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # str|int|float|bool|list
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now, nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("ix_settings_category", "category"),)


class AuditLog(Base):
    """Append-only history of settings changes — every set/reset writes a row.

    Provides a who/what/when answer for any "why did the rate limit change?"
    follow-up. At scale, partition by month and tier off to columnar storage.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(default=_now, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # set | reset | bulk_update
    setting_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    old_value: Mapped[Optional[object]] = mapped_column(JSON, nullable=True)
    new_value: Mapped[Optional[object]] = mapped_column(JSON, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
