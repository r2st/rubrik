"""Tests for the LLM Tier-2 gateway scaffold."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_llm_settings():
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    rt.set("llm.tier2_enabled", False, actor="test")
    rt.set("llm.tier2_api_key", "", actor="test")
    rt.set("llm.tier2_daily_budget_usd", 50.0, actor="test")
    yield
    rt.set("llm.tier2_enabled", False, actor="test-cleanup")
    rt.set("llm.tier2_api_key", "", actor="test-cleanup")


def test_gateway_disabled_raises():
    from api.llm_gateway import FrontierGateway
    g = FrontierGateway()
    with pytest.raises(RuntimeError, match="disabled"):
        g.call(prompt="hi", tenant="t1")


def test_gateway_enabled_without_key_raises():
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    rt.set("llm.tier2_enabled", True, actor="test")

    from api.llm_gateway import FrontierGateway
    g = FrontierGateway()
    with pytest.raises(RuntimeError, match="API key"):
        g.call(prompt="hi", tenant="t1")


def test_gateway_refuses_mostly_redacted_payload():
    """If >50% of the prompt is redacted, fail closed."""
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    rt.set("llm.tier2_enabled", True, actor="test")
    rt.set("llm.tier2_api_key", "sk-test-fake-key", actor="test")

    from api.llm_gateway import BudgetExceeded, FrontierGateway
    # Mostly emails — redacting them strips most of the content.
    payload = " ".join([f"u{i}@x.com" for i in range(20)])
    g = FrontierGateway()
    with pytest.raises(BudgetExceeded, match="50%"):
        g.call(prompt=payload, tenant="t1")


def test_estimate_cost_is_deterministic():
    from api.llm_gateway import FrontierGateway
    g = FrontierGateway()
    a = g._estimate_cost("anthropic", "claude", prompt_len=4000, completion_len=1000)
    b = g._estimate_cost("anthropic", "claude", prompt_len=4000, completion_len=1000)
    assert a == b
    assert a > 0


def test_provider_call_requires_api_key():
    """No API key configured → fail loud with a useful error rather
    than silently degrade."""
    from api.llm_gateway import FrontierGateway
    g = FrontierGateway()
    cfg = {"provider": "anthropic", "model": "claude", "api_key": "", "timeout_s": 30}
    with pytest.raises(RuntimeError, match="no API key configured"):
        g._call_provider(cfg, "hi", 100)


def test_provider_call_unknown_provider():
    """Unknown provider name → loud failure, not a confusing import error."""
    from api.llm_gateway import FrontierGateway
    g = FrontierGateway()
    cfg = {"provider": "made-up", "model": "x", "api_key": "k", "timeout_s": 30}
    with pytest.raises(RuntimeError, match="Unknown Tier-2 provider"):
        g._call_provider(cfg, "hi", 100)


def test_response_cache_replays_identical_call():
    """Two identical calls — second one returns the cached envelope (no
    provider call, latency 0, cost 0)."""
    from src.runtime_settings import get_runtime
    rt = get_runtime()
    rt.set("llm.tier2_enabled", True, actor="test")
    rt.set("llm.tier2_api_key", "sk-test-fake-key", actor="test")

    from api import cache as cache_mod
    cache_mod._local._d.clear()
    # Pre-seed the response cache so the first call sees a hit. We
    # simulate "we already paid for this answer, replay it."
    from api.llm_gateway import FrontierGateway
    g = FrontierGateway()
    redacted_prompt = "summarise this meeting please"
    cfg_for_key = g._config()
    key = cache_mod.content_hash(
        cfg_for_key["provider"], cfg_for_key["model"],
        redacted_prompt, 1024,
    )
    import json as _json
    cache_mod.cache_set(
        "llm_tier2_response", key,
        _json.dumps({"text": "cached answer", "model": cfg_for_key["model"]}),
        ttl_seconds=3600,
    )
    resp = g.call(prompt=redacted_prompt, tenant="t1")
    assert resp.text == "cached answer"
    assert resp.latency_ms == 0.0
    assert resp.estimated_cost_usd == 0.0
