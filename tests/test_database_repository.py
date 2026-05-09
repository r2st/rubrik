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
