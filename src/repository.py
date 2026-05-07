"""Transcript repository — abstraction over the data source.

The original `data_loader.load_all_meetings()` reads every meeting JSON file
into memory. That's correct for small datasets; it fails at 100M+
records. This module introduces a `TranscriptRepository` Protocol so the
analytical pipeline can run against any backend that implements it:

  - **LocalDirectoryRepository** — the original development-volume behavior; reads
    JSON dirs from disk. Default for dev / take-home.
  - **DatabaseRepository** — production path; reads from Postgres + Iceberg.
    Not implemented here; documented in ADR 0008 + 0011 as the swap target.
  - **KafkaStreamingRepository** — for real-time pipelines. Same.

The point of the abstraction is that the rest of the pipeline doesn't know
or care which backend is in use. `streaming.py` consumes any repository
that implements the protocol.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional, Protocol, runtime_checkable

from .data_loader import Meeting, load_meeting
from .logging_config import get_logger
from .settings import get_settings

log = get_logger(__name__)


@runtime_checkable
class TranscriptRepository(Protocol):
    """Read-only interface for fetching meetings.

    Implementations must support both batch eager-load (for backward
    compatibility with the in-memory pipeline) and streaming iteration
    (for production volume).
    """

    def count(self) -> int:
        """Total number of meetings available."""
        ...

    def get(self, meeting_id: str) -> Optional[Meeting]:
        """Single-meeting lookup. None if not found."""
        ...

    def stream(self, *, batch_size: int = 1000) -> Iterator[list[Meeting]]:
        """Yield meetings in batches. Never holds the full dataset in memory."""
        ...

    def all(self) -> list[Meeting]:
        """Eager load everything. Convenience for small datasets; do NOT
        call at production volume — use `stream()` instead."""
        ...


# ---------------------------------------------------------------------------
# Backend: local directory of JSON files (the development-volume default)
# ---------------------------------------------------------------------------
class LocalDirectoryRepository:
    """Reads meeting directories from a local filesystem path.

    Comfortable up to ~100k meetings (filesystem inode + load latency become
    the bottleneck somewhere around there). Above that, switch to a
    `DatabaseRepository` reading from Postgres + Iceberg per ADR 0008.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        from . import config
        self.root = root or config.DATASET_PATH
        if not self.root.exists():
            raise FileNotFoundError(f"Dataset path not found: {self.root}")

    def count(self) -> int:
        return sum(1 for p in self.root.iterdir() if p.is_dir())

    def get(self, meeting_id: str) -> Optional[Meeting]:
        target = self.root / meeting_id
        if not target.is_dir():
            return None
        return load_meeting(target)

    def stream(self, *, batch_size: int = 1000) -> Iterator[list[Meeting]]:
        """Yield meetings in fixed-size batches. The batch is materialized
        in memory but the full set never is, so this works at any scale the
        local filesystem can hold."""
        batch: list[Meeting] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            batch.append(load_meeting(entry))
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def all(self) -> list[Meeting]:
        return [m for batch in self.stream(batch_size=10_000) for m in batch]


# ---------------------------------------------------------------------------
# Default-instance helper
# ---------------------------------------------------------------------------
def default_repository() -> TranscriptRepository:
    """Build the default repository for the current environment.

    Today: `LocalDirectoryRepository` over the path in `bootstrap.toml`.
    Production: swap to a `DatabaseRepository` (per ADR 0008/0011) by changing
    only this function.
    """
    settings = get_settings()
    if settings.dataset_path is not None:
        return LocalDirectoryRepository(root=Path(settings.dataset_path))
    return LocalDirectoryRepository()
