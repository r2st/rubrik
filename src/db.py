"""SQLAlchemy engine + session factory.

Single source of truth for DB connections. Defaults to SQLite for dev (no
external deps); swap the URL in `bootstrap.toml` to Postgres in production —
no code changes required.

The engine is lazy-initialized at first use to avoid creating files just by
importing this module (matters for tests).
"""
from __future__ import annotations

import threading
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .logging_config import get_logger
from .settings import get_settings

log = get_logger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_lock = threading.Lock()


class Base(DeclarativeBase):
    """Common base for all ORM models."""


def _resolve_url(url: str) -> str:
    """Make sure the SQLite parent dir exists so engine creation doesn't fail."""
    if url.startswith("sqlite"):
        # Strip dialect prefix to get the file path
        # e.g. sqlite:///./data/admin.db  →  ./data/admin.db
        path_part = url.split("///", 1)[-1]
        if path_part and path_part != ":memory:":
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)
    return url


def get_engine() -> Engine:
    """Return the singleton SQLAlchemy engine, building on first call."""
    global _engine, _session_factory
    if _engine is None:
        with _lock:
            if _engine is None:
                url = _resolve_url(get_settings().database_url)
                kwargs: dict = {"echo": False, "future": True}
                if url.startswith("sqlite"):
                    # Allow connection sharing across the FastAPI threadpool
                    kwargs["connect_args"] = {"check_same_thread": False}
                _engine = create_engine(url, **kwargs)
                _session_factory = sessionmaker(
                    bind=_engine, expire_on_commit=False, class_=Session,
                )
                log.info("Database engine ready: %s", url.split("@")[-1])
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        get_engine()  # initializes both
    assert _session_factory is not None
    return _session_factory


def session_scope() -> Session:
    """Return a new Session — caller is responsible for commit/close."""
    return get_session_factory()()


def init_db() -> None:
    """Create all tables registered via the Base metadata. Safe to call repeatedly."""
    from . import models_db  # noqa: F401  — register tables

    engine = get_engine()
    Base.metadata.create_all(engine)
    log.info("Database tables ensured")


def reset_for_tests() -> None:
    """Drop the engine so the next call rebuilds it (e.g., after switching URL)."""
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None
