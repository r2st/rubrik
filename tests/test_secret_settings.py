"""Tests for the ``secret`` runtime-setting type — masked on read, in the
audit log, and through the admin API.

The LLM Tier-2 API key (``llm.tier2_api_key``) is the load-bearing user-
facing example. Operators can rotate it through ``/admin``; the raw value
must never appear in API responses or audit-log rows.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.runtime_settings import get_runtime, initialize_db_and_seed, mask_secret

BOOTSTRAP_PASSWORD = "changeme-on-first-login"


@pytest.fixture(autouse=True)
def _ensure_db_seeded():
    """The runtime store reads from the admin DB; seed it for unit tests
    that don't go through the FastAPI lifespan."""
    initialize_db_and_seed()
    yield


# ---------------------------------------------------------------------------
# mask_secret unit
# ---------------------------------------------------------------------------
def test_mask_secret_empty_returns_empty():
    assert mask_secret("") == ""
    assert mask_secret(None) == ""


def test_mask_secret_short_value_fully_masked():
    """≤ 4 chars never reveal any tail — the whole value is dots."""
    assert mask_secret("abc") == "•••"
    assert mask_secret("1234") == "••••"


def test_mask_secret_long_value_shows_last_four():
    assert mask_secret("sk-ant-api03-thisIsTheKey-abcd") == "••••••abcd"


# ---------------------------------------------------------------------------
# Setting catalogue — the LLM keys are seeded
# ---------------------------------------------------------------------------
def test_llm_settings_seeded():
    rt = get_runtime()
    keys = {s.key for s in rt.all()}
    expected = [
        "llm.tier2_enabled",
        "llm.tier2_provider",
        "llm.tier2_model",
        "llm.tier2_api_key",
        "llm.tier2_daily_budget_usd",
        "llm.tier2_request_timeout_s",
        "llm.tier1_endpoint",
    ]
    for k in expected:
        assert k in keys, f"missing default: {k}"


def test_llm_api_key_default_is_secret_typed():
    rt = get_runtime()
    s = next(s for s in rt.all() if s.key == "llm.tier2_api_key")
    assert s.type == "secret"


# ---------------------------------------------------------------------------
# Storage round-trip — runtime store keeps the raw value
# ---------------------------------------------------------------------------
def test_runtime_store_keeps_raw_secret():
    """Application code reads the actual key, not the masked form."""
    rt = get_runtime()
    rt.set("llm.tier2_api_key", "sk-fake-api03-rotateMe-XyZw", actor="test")
    try:
        assert rt.get("llm.tier2_api_key") == "sk-fake-api03-rotateMe-XyZw"
    finally:
        rt.set("llm.tier2_api_key", "", actor="test-cleanup")


# ---------------------------------------------------------------------------
# Admin API — masked on every read path
# ---------------------------------------------------------------------------
def _login(c: TestClient) -> None:
    r = c.post("/api/v1/admin/login", json={"password": BOOTSTRAP_PASSWORD})
    assert r.status_code == 200, r.text


def test_admin_list_settings_masks_secret_value():
    from api.main import app
    rt = get_runtime()
    rt.set("llm.tier2_api_key", "sk-fake-api03-very-secret-key-1234",
           actor="test")
    try:
        with TestClient(app) as c:
            _login(c)
            r = c.get("/api/v1/admin/settings")
            assert r.status_code == 200
            llm_block = next(b for b in r.json() if b["category"] == "llm")
            api_key_setting = next(
                s for s in llm_block["settings"] if s["key"] == "llm.tier2_api_key"
            )
        # Masked form, never the raw value.
        assert api_key_setting["value"] == "••••••1234"
        assert "very-secret" not in api_key_setting["value"]
    finally:
        rt.set("llm.tier2_api_key", "", actor="test-cleanup")


def test_admin_get_setting_masks_secret_value():
    from api.main import app
    rt = get_runtime()
    rt.set("llm.tier2_api_key", "sk-fake-thisIsTheActualKey", actor="test")
    try:
        with TestClient(app) as c:
            _login(c)
            r = c.get("/api/v1/admin/settings/llm.tier2_api_key")
            assert r.status_code == 200, r.text
            assert r.json()["value"] == "••••••lKey"
    finally:
        rt.set("llm.tier2_api_key", "", actor="test-cleanup")


def test_admin_update_setting_returns_masked_value():
    """Rotating the key returns the masked form, not what was sent in."""
    from api.main import app
    rt = get_runtime()
    try:
        with TestClient(app) as c:
            _login(c)
            r = c.put(
                "/api/v1/admin/settings/llm.tier2_api_key",
                json={"value": "sk-rotated-fake-1234"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["value"] == "••••••1234"
            # And the raw value must not have leaked anywhere in the body.
            assert "rotated-fake" not in r.text
    finally:
        rt.set("llm.tier2_api_key", "", actor="test-cleanup")


# ---------------------------------------------------------------------------
# Audit log — never stores the raw secret
# ---------------------------------------------------------------------------
def test_audit_log_stores_masked_old_and_new_for_secret():
    """Both old_value and new_value in audit_log are masked when type=secret."""
    rt = get_runtime()
    rt.set("llm.tier2_api_key", "sk-old-real-key-aaaa", actor="test")
    rt.set("llm.tier2_api_key", "sk-new-real-key-bbbb", actor="test")
    try:
        recent = rt.audit_log(limit=10)
        rotation_entries = [
            e for e in recent
            if e.setting_key == "llm.tier2_api_key" and e.action == "set"
        ]
        assert len(rotation_entries) >= 2
        for e in rotation_entries:
            new_v = str(e.new_value or "")
            old_v = str(e.old_value or "")
            assert "real-key" not in new_v, f"raw secret leaked in audit: {new_v}"
            assert "real-key" not in old_v, f"raw secret leaked in audit: {old_v}"
            # And it should be the masked form (or empty for the first set).
            if new_v:
                assert new_v.startswith("••••••"), new_v
    finally:
        rt.set("llm.tier2_api_key", "", actor="test-cleanup")
