"""Shared fixtures for the test suite."""
from __future__ import annotations

import pandas as pd
import pytest

from src import categorizer, data_loader, sentiment


@pytest.fixture(scope="session")
def raw_meetings():
    """Load all 100 meetings from the dataset (session-scoped)."""
    return data_loader.load_all_meetings()


@pytest.fixture(scope="session")
def meetings_df(raw_meetings) -> pd.DataFrame:
    """Annotated meetings DataFrame with categorization + trajectories."""
    df = data_loader.meetings_to_dataframe(raw_meetings)
    sentences_df = data_loader.sentences_dataframe(raw_meetings)
    df = categorizer.annotate(df)
    df["num_action_items"] = df["action_items"].apply(len)
    df = sentiment.add_trajectories(df, sentences_df)
    return df


@pytest.fixture(scope="session")
def sentences_df(raw_meetings) -> pd.DataFrame:
    return data_loader.sentences_dataframe(raw_meetings)


@pytest.fixture(scope="session")
def speakers_df(raw_meetings) -> pd.DataFrame:
    return data_loader.speakers_dataframe(raw_meetings)


@pytest.fixture(autouse=True)
def _reset_admin_strict_rate_limit():
    """Clear the per-process strict + write rate-limit buckets between tests.

    Production behavior is per-IP/minute; tests issue many requests from the
    same loopback address in rapid succession, which would otherwise trip the
    limiter and turn unrelated assertions into 429s.
    """
    from api.admin.routes import _strict_window, _write_window
    _strict_window.clear()
    _write_window.clear()
    yield
    _strict_window.clear()
    _write_window.clear()


@pytest.fixture(autouse=True)
def _disable_csrf_by_default():
    """Tests don't go through the login → cookie → header round-trip on
    every PUT/POST. Default CSRF off; tests that exercise the CSRF path
    explicitly enable it via their own fixture."""
    try:
        from src.runtime_settings import get_runtime, initialize_db_and_seed
        initialize_db_and_seed()
        get_runtime().set("auth.csrf_enabled", False, actor="conftest")
    except Exception:  # noqa: BLE001 — bootstrap order during collection
        pass
    yield
