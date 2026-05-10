"""Tests for DatabaseRepository (SQL-backed transcript repository).

Uses SQLite (the dev backend) so tests are fast. The same code path runs
against Postgres in production — the SQL is portable.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from src.db import session_scope
from src.repository import DatabaseRepository, _meeting_to_dict


@pytest.fixture
def meetings_table():
    """Create a portable meetings table for the test, drop on teardown."""
    with session_scope() as s:
        s.execute(text("DROP TABLE IF EXISTS meetings"))
        s.execute(text(
            "CREATE TABLE meetings ("
            "  meeting_id VARCHAR(128) PRIMARY KEY, "
            "  raw TEXT NOT NULL, "
            "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        ))
        s.commit()
    yield
    with session_scope() as s:
        s.execute(text("DROP TABLE IF EXISTS meetings"))
        s.commit()


def _seed(meeting_id: str, raw: dict) -> None:
    import json
    with session_scope() as s:
        s.execute(
            text("INSERT INTO meetings (meeting_id, raw) VALUES (:m, :r)"),
            {"m": meeting_id, "r": json.dumps(raw)},
        )
        s.commit()


def test_count_zero_then_seeded(meetings_table):
    repo = DatabaseRepository()
    assert repo.count() == 0
    _seed("m-1", {"info": {}, "transcript": {}, "summary": {}})
    assert repo.count() == 1


def test_get_returns_none_for_missing(meetings_table):
    repo = DatabaseRepository()
    assert repo.get("nope") is None


def test_get_round_trip(meetings_table):
    """Stored blob round-trips into a Meeting dataclass."""
    raw = {
        "info": {"title": "Acme — Q3 review"},
        "transcript": {"data": [{"sentence": "Hello", "sentiment": "positive"}]},
        "summary": {"sentimentScore": 4.0},
        "speakers": [{"name": "alice"}],
        "speaker_meta": {"timezone": "UTC"},
        "events": [{"type": "churn_signal"}],
    }
    _seed("m-1", raw)
    repo = DatabaseRepository()
    m = repo.get("m-1")
    assert m is not None
    assert m.meeting_id == "m-1"
    assert m.title == "Acme — Q3 review"
    assert m.summary["sentimentScore"] == 4.0
    assert len(m.sentences) == 1


def test_stream_yields_in_batches(meetings_table):
    for i in range(5):
        _seed(f"m-{i:02d}", {"info": {}, "transcript": {}, "summary": {}})
    repo = DatabaseRepository()
    batches = list(repo.stream(batch_size=2))
    sizes = [len(b) for b in batches]
    assert sum(sizes) == 5
    assert max(sizes) <= 2


def test_all_returns_full_set(meetings_table):
    for i in range(3):
        _seed(f"m-{i}", {"info": {}, "transcript": {}, "summary": {}})
    assert len(DatabaseRepository().all()) == 3


def test_meeting_to_dict_round_trip():
    """Dataclass → dict → dataclass preserves every field."""
    from src.data_loader import Meeting

    original = Meeting(
        meeting_id="m-x",
        info={"title": "t"},
        transcript={"data": [{"sentence": "s", "sentiment": "neutral"}]},
        speakers=[{"name": "a"}],
        speaker_meta={"tz": "UTC"},
        summary={"sentimentScore": 3.5},
        events=[{"type": "blocker"}],
    )
    d = _meeting_to_dict(original)
    rebuilt = DatabaseRepository._row_to_meeting("m-x", d)
    assert rebuilt.meeting_id == "m-x"
    assert rebuilt.title == "t"
    assert rebuilt.summary["sentimentScore"] == 3.5
    assert rebuilt.speakers == [{"name": "a"}]


def test_default_repository_picks_database_when_setting_is_set():
    from src.repository import (
        DatabaseRepository as DB,
    )
    from src.repository import (
        LocalDirectoryRepository,
        default_repository,
    )
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    rt.set("transcripts.repository", "database", actor="test")
    try:
        repo = default_repository()
        assert isinstance(repo, DB)
    finally:
        rt.set("transcripts.repository", "local", actor="test-cleanup")
    # Sanity: with the setting back to 'local' we get the filesystem repo.
    assert isinstance(default_repository(), LocalDirectoryRepository)


# ---------------------------------------------------------------------------
# delete() + per-tenant filter (added to support the GDPR path + multi-tenancy)
# ---------------------------------------------------------------------------
@pytest.fixture
def meetings_table_with_tenant():
    """Variant of the fixture with a ``tenant_id`` column."""
    with session_scope() as s:
        s.execute(text("DROP TABLE IF EXISTS meetings"))
        s.execute(text(
            "CREATE TABLE meetings ("
            "  meeting_id VARCHAR(128) PRIMARY KEY, "
            "  tenant_id VARCHAR(64), "
            "  raw TEXT NOT NULL, "
            "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        ))
        s.commit()
    yield
    with session_scope() as s:
        s.execute(text("DROP TABLE IF EXISTS meetings"))
        s.commit()


def _seed_tenant(meeting_id: str, tenant_id: str, raw: dict) -> None:
    import json
    with session_scope() as s:
        s.execute(
            text(
                "INSERT INTO meetings (meeting_id, tenant_id, raw) "
                "VALUES (:m, :t, :r)"
            ),
            {"m": meeting_id, "t": tenant_id, "r": json.dumps(raw)},
        )
        s.commit()


def test_delete_removes_row_and_returns_true(meetings_table):
    repo = DatabaseRepository()
    _seed("m-del-1", {"info": {}, "transcript": {}, "summary": {}})
    assert repo.count() == 1
    assert repo.delete("m-del-1") is True
    assert repo.count() == 0
    # Second delete is a no-op and returns False.
    assert repo.delete("m-del-1") is False


def test_get_with_tenant_filter_isolates_rows(meetings_table_with_tenant):
    """Tenant A must NEVER see tenant B's meeting via id-guessing."""
    repo = DatabaseRepository()
    _seed_tenant("m-shared", "tenant-a", {"info": {"customer": "Acme"}})
    _seed_tenant("m-other",  "tenant-b", {"info": {"customer": "Beta"}})

    # Without filter: both reachable (single-tenant compatibility path).
    assert repo.get("m-shared") is not None
    assert repo.get("m-other") is not None

    # With filter: each tenant sees only their own row.
    assert repo.get("m-shared", tenant_id="tenant-a") is not None
    assert repo.get("m-shared", tenant_id="tenant-b") is None
    assert repo.get("m-other",  tenant_id="tenant-b") is not None
    assert repo.get("m-other",  tenant_id="tenant-a") is None


def test_local_repository_delete_raises_not_implemented(tmp_path):
    """Filesystem layout is read-only; deletion must surface as a clear error."""
    from src.repository import LocalDirectoryRepository
    (tmp_path / "m-1").mkdir()
    repo = LocalDirectoryRepository(root=tmp_path)
    with pytest.raises(NotImplementedError, match="read-only"):
        repo.delete("m-1")
