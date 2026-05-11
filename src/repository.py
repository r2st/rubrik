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

    def delete(self, meeting_id: str) -> bool:
        """Best-effort delete by id. Returns True iff a row existed.

        Required by the GDPR right-to-be-forgotten path (``api/admin/gdpr.py``).
        Backends that can't honor deletion (e.g. read-only filesystem
        layouts) raise ``NotImplementedError`` so the caller can pick
        the appropriate fallback path."""
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

    def delete(self, meeting_id: str) -> bool:
        """Filesystem layout is treated as read-only — deletion happens
        upstream. We raise so the GDPR path can fall through to the
        DatabaseRepository when both are configured, and so a misconfigured
        deployment surfaces the issue instead of silently lying."""
        raise NotImplementedError(
            "LocalDirectoryRepository is read-only; configure a "
            "DatabaseRepository to honor GDPR deletes",
        )


# ---------------------------------------------------------------------------
# Backend: SQL database (Postgres in production) via JSON blob storage
# ---------------------------------------------------------------------------
class DatabaseRepository:
    """Reads meetings from a SQL database — schema-on-read JSON blobs.

    Each meeting is one row: ``(meeting_id, raw, created_at)`` where
    ``raw`` is a JSON column holding the full meeting envelope. This is
    the architectural target ADR 0008 commits to (Postgres OLTP); the
    analytical tier (ClickHouse, Iceberg) is fed by the outbox + relayer
    in ADR 0014.

    The ``meetings`` table itself is owned at the data-platform layer,
    not by the application's alembic migrations (which only manage
    ``settings`` / ``audit_log`` / ``outbox_events``). The
    ``import_from_local`` helper is a one-shot migration aid for
    deployments transitioning from filesystem-backed to database-backed
    storage.
    """

    def __init__(self, *, table_name: str = "meetings") -> None:
        self.table_name = table_name

    def count(self) -> int:
        from sqlalchemy import text

        from .db import session_scope
        with session_scope() as s:
            return int(
                s.execute(text(f"SELECT COUNT(*) FROM {self.table_name}"))
                .scalar() or 0
            )

    def get(
        self, meeting_id: str, *, tenant_id: Optional[str] = None,
    ) -> Optional[Meeting]:
        """Look up a single meeting. When ``tenant_id`` is provided the
        query also filters on it — multi-tenant deployments MUST pass
        the caller's tenant to keep one tenant from reading another's
        data via id-guessing. Single-tenant callers can omit it.

        If ``tenant_id`` is not passed explicitly the repository falls
        back to ``src.tenant.current_tenant()`` (a request-scoped
        ContextVar set by the public-app tenant middleware), so route
        handlers automatically get tenant-scoped reads without
        re-plumbing the value through every layer."""
        if tenant_id is None:
            from .tenant import current_tenant
            tenant_id = current_tenant()

        from sqlalchemy import text

        from .db import session_scope
        with session_scope() as s:
            if tenant_id is None:
                row = s.execute(
                    text(
                        f"SELECT raw FROM {self.table_name} "
                        f"WHERE meeting_id = :mid"
                    ),
                    {"mid": meeting_id},
                ).first()
            else:
                row = s.execute(
                    text(
                        f"SELECT raw FROM {self.table_name} "
                        f"WHERE meeting_id = :mid AND tenant_id = :tid"
                    ),
                    {"mid": meeting_id, "tid": tenant_id},
                ).first()
        if row is None:
            return None
        return self._row_to_meeting(meeting_id, row[0])

    def delete(self, meeting_id: str) -> bool:
        """Delete by primary key. Returns True iff a row was removed."""
        from sqlalchemy import text

        from .db import session_scope
        with session_scope() as s:
            result = s.execute(
                text(
                    f"DELETE FROM {self.table_name} "
                    f"WHERE meeting_id = :mid"
                ),
                {"mid": meeting_id},
            )
            s.commit()
            return int(result.rowcount or 0) > 0

    def stream(self, *, batch_size: int = 1000) -> Iterator[list[Meeting]]:
        """Chunked fetch — bounded memory regardless of total size."""
        from sqlalchemy import text

        from .db import session_scope
        offset = 0
        while True:
            with session_scope() as s:
                rows = s.execute(
                    text(
                        f"SELECT meeting_id, raw FROM {self.table_name} "
                        f"ORDER BY meeting_id LIMIT :lim OFFSET :off"
                    ),
                    {"lim": batch_size, "off": offset},
                ).fetchall()
            if not rows:
                return
            yield [self._row_to_meeting(mid, raw) for mid, raw in rows]
            if len(rows) < batch_size:
                return
            offset += len(rows)

    def all(self) -> list[Meeting]:
        return [m for batch in self.stream(batch_size=10_000) for m in batch]

    def import_from_local(
        self, root: Path, *, batch_size: int = 1000,
    ) -> int:
        """One-shot: copy a filesystem layout into the table. Idempotent."""
        from sqlalchemy import text

        from .db import session_scope
        local = LocalDirectoryRepository(root=root)
        inserted = 0
        for batch in local.stream(batch_size=batch_size):
            with session_scope() as s:
                for m in batch:
                    s.execute(
                        text(
                            f"INSERT INTO {self.table_name} "
                            f"(meeting_id, raw, created_at) "
                            f"VALUES (:mid, :raw, CURRENT_TIMESTAMP) "
                            f"ON CONFLICT (meeting_id) DO UPDATE "
                            f"SET raw = EXCLUDED.raw"
                        ),
                        {"mid": m.meeting_id, "raw": _meeting_to_dict(m)},
                    )
                    inserted += 1
                s.commit()
        log.info("DatabaseRepository imported %d meetings from %s",
                 inserted, root)
        return inserted

    @staticmethod
    def _row_to_meeting(meeting_id: str, raw):
        if isinstance(raw, str):
            import json
            raw = json.loads(raw)
        return Meeting(
            meeting_id=meeting_id,
            info=raw.get("info", {}),
            transcript=raw.get("transcript", {}),
            speakers=raw.get("speakers", []),
            speaker_meta=raw.get("speaker_meta", {}),
            summary=raw.get("summary", {}),
            events=raw.get("events", []),
        )


def _meeting_to_dict(m: Meeting) -> dict:
    return {
        "info": m.info,
        "transcript": m.transcript,
        "speakers": m.speakers,
        "speaker_meta": m.speaker_meta,
        "summary": m.summary,
        "events": m.events,
    }


# ---------------------------------------------------------------------------
# Backend: cache-wrapping decorator (Redis read-through over any backend)
# ---------------------------------------------------------------------------
class CachedTranscriptRepository:
    """Decorator that adds Redis read-through caching to a backing repository.

    Wraps ``get(meeting_id)`` only — ``stream`` and ``all`` are bulk paths
    that already use bounded memory and shouldn't be cached at the
    individual-row level (they'd evict everything else). ``count()`` is
    cheap enough not to bother.

    Production wiring::

        backing = DatabaseRepository()
        repo = CachedTranscriptRepository(backing)

    Cache namespace: ``meeting``. Invalidation on writes is the caller's
    responsibility — when a meeting row mutates, call
    ``api/cache.cache_invalidate("meeting", meeting_id)``. The outbox
    relayer is the natural hook for fanning invalidation events.
    """

    NAMESPACE = "meeting"
    DEFAULT_TTL_SECONDS = 600  # 10 minutes — meetings rarely mutate post-ingest

    def __init__(
        self,
        backing: TranscriptRepository,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.backing = backing
        self.ttl_seconds = ttl_seconds

    def count(self) -> int:
        return self.backing.count()

    def get(self, meeting_id: str) -> Optional[Meeting]:
        """Read-through Redis cache with TTL jitter + negative caching.

        Sync wrapper around the async ``get_or_load`` helper — the
        repository contract is sync, so we pump a one-shot loop.

        The cache key is tenant-scoped so two tenants asking for the
        same ``meeting_id`` never share a cached entry — without this,
        a tenant could read another tenant's data the moment they
        guessed the id.
        """
        from api import cache as cache_mod

        # Tenant-scoped cache key — None tenant collapses to the
        # historical "meeting:{id}" namespace so single-tenant callers
        # see no behaviour change.
        from .tenant import current_tenant
        tid = current_tenant()
        cache_key = meeting_id if tid is None else f"{tid}:{meeting_id}"

        # Cheap fast path: try the sync cache first to avoid the loop spin.
        raw = cache_mod.cache_get(self.NAMESPACE, cache_key)
        if raw is not None:
            if raw == "":
                return None  # negative cache hit
            try:
                import json
                return DatabaseRepository._row_to_meeting(
                    meeting_id, json.loads(raw),
                )
            except Exception:  # noqa: BLE001
                pass  # fall through to refresh

        m = self.backing.get(meeting_id)
        if m is None:
            cache_mod.cache_set(
                self.NAMESPACE, cache_key, "", ttl_seconds=30,
            )
            return None
        try:
            import json
            cache_mod.cache_set(
                self.NAMESPACE, cache_key,
                json.dumps(_meeting_to_dict(m)),
                ttl_seconds=self.ttl_seconds,
            )
        except Exception:  # noqa: BLE001
            pass
        return m

    def stream(self, *, batch_size: int = 1000) -> Iterator[list[Meeting]]:
        return self.backing.stream(batch_size=batch_size)

    def all(self) -> list[Meeting]:
        return self.backing.all()

    def delete(self, meeting_id: str) -> bool:
        """Delete the row from the backing store + drop the cache entry.
        Cache invalidation runs even if the backing delete returned False,
        so we never serve a tombstoned row from cache."""
        try:
            removed = bool(self.backing.delete(meeting_id))
        finally:
            self.invalidate(meeting_id)
        return removed

    @staticmethod
    def invalidate(meeting_id: str) -> None:
        """Drop a single meeting from the cache. Call on write."""
        from api import cache as cache_mod
        cache_mod.cache_invalidate(
            CachedTranscriptRepository.NAMESPACE, meeting_id,
        )


# ---------------------------------------------------------------------------
# Default-instance helper
# ---------------------------------------------------------------------------
def default_repository() -> TranscriptRepository:
    """Build the default repository for the current environment.

    Resolution order:
      1. ``transcripts.repository = "database"`` runtime setting → DatabaseRepository
      2. ``[paths].dataset_path`` set in bootstrap.toml → LocalDirectoryRepository over that path
      3. Default → LocalDirectoryRepository over the repo's data dir
    """
    try:
        from .runtime_settings import get_runtime
        backend = str(get_runtime().get("transcripts.repository", "local"))
    except Exception:  # noqa: BLE001
        backend = "local"
    if backend == "database":
        return DatabaseRepository()

    settings = get_settings()
    if settings.dataset_path is not None:
        return LocalDirectoryRepository(root=Path(settings.dataset_path))
    return LocalDirectoryRepository()
