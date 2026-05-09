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


def test_provider_call_not_implemented_yet():
    """The provider HTTP layer is stubbed — production wires the real client."""
    from api.llm_gateway import FrontierGateway
    g = FrontierGateway()
    cfg = {"provider": "anthropic", "model": "claude"}
    with pytest.raises(NotImplementedError, match="not wired"):
        g._call_provider(cfg, "hi", 100)
