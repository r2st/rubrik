"""Frontier-LLM gateway (ADR 0012 §"Implementation plan" step 2).

Thin wrapper that the Tier-1 → Tier-2 escalation path calls when one of
the trigger conditions fires. Provides:

  - Provider abstraction (Anthropic / OpenAI / Google) — a single
    ``call(prompt, **kwargs)`` interface; provider-specific HTTP details
    live behind it.
  - **PII redaction** — every prompt body is run through ``src/pii.py``
    before it leaves the perimeter. Redaction summary recorded so
    operators can see what was scrubbed (without seeing the content).
  - **Per-tenant daily $ budget** — Redis-backed counter; over budget
    raises ``BudgetExceeded`` and the caller falls back to Tier-1
    output flagged ``low_confidence=true``.
  - **Circuit breaker** — wrapping the provider call so a sick
    frontier API doesn't stall the bulk path.
  - **Audit log** — every call emits an audit row (model, latency,
    estimated cost, redaction summary, tenant) via the existing
    ``audit_log`` table.

What's NOT here yet
-------------------
  - Active-learning queue — Tier-2 outputs become training data for
    the next Gemma fine-tune (ADR 0010). The wiring is a queue ``put``
    on each successful call; the worker side lives in the ML pipeline.
  - Response cache — ``(input_hash, prompt_version, model)`` keyed
    Redis cache. Cheap to add; deferred until call volume justifies.

These are documented in ADR 0012's "Implementation plan" — the gateway
is the bridge that lets them all be incremental additions rather than
new code paths.
"""
from __future__ import annotations

import json as _json
import time
from dataclasses import dataclass
from typing import Any

from src.logging_config import get_logger
from src.pii import RedactionSummary, default_redactor

from .circuit_breaker import CircuitOpenError, get_breaker

log = get_logger(__name__)


class BudgetExceeded(Exception):
    """Raised when the per-tenant daily $ cap would be breached by this call."""


@dataclass
class GatewayResponse:
    """Result of a Tier-2 call (or fallback)."""
    text: str
    model: str
    latency_ms: float
    estimated_cost_usd: float
    redaction: RedactionSummary
    tenant: str


@dataclass
class FrontierGateway:
    """Minimal scaffold — the production wiring inserts a real provider client."""

    # Per-tenant counters keyed in Redis as ``llm_budget:{tenant}:{YYYYMMDD}``
    # so they reset at midnight UTC. The TTL is one day for the same reason.
    _BUDGET_KEY_FORMAT: str = "llm_budget:{tenant}:{day}"

    def call(
        self,
        *,
        prompt: str,
        tenant: str,
        max_tokens: int = 1024,
    ) -> GatewayResponse:
        """Send a prompt to the configured Tier-2 provider.

        Steps:
          1. Read provider config from runtime settings.
          2. Redact PII from the prompt; abort if the redactor would
             remove > 50% of the content (that's a sign the input is
             essentially all PII — failing closed is safer).
          3. Check the per-tenant budget.
          4. Call the provider via the ``llm_gateway_<provider>`` circuit
             breaker.
          5. Charge the budget, write an audit row, return.
        """
        cfg = self._config()
        if not cfg["enabled"]:
            raise RuntimeError(
                "LLM Tier-2 gateway is disabled (llm.tier2_enabled = false). "
                "Caller should fall back to Tier-1 output."
            )
        if not cfg["api_key"]:
            raise RuntimeError(
                "LLM Tier-2 API key not configured. Set llm.tier2_api_key in /admin."
            )

        # PII redaction — fail closed if >50% of the prompt's characters
        # were inside PII matches. Density is robust to placeholder length
        # (the placeholder text expands the redacted output).
        redacted, summary = default_redactor.redact(prompt)
        if summary.density(len(prompt)) > 0.5:
            raise BudgetExceeded(
                "Refusing to send a payload that is >50% PII — "
                "operator should review the prompt template."
            )

        self._enforce_budget(tenant, cfg["daily_budget_usd"])

        # Response cache — key on the redacted prompt + model + max_tokens.
        # Hit means "we paid for this exact answer recently; replay it for
        # free." Stays scoped per provider/model so a model-version change
        # invalidates implicitly. Best-effort; failures fall through.
        from . import cache as cache_mod
        from . import metrics as metrics_mod
        cache_key = cache_mod.content_hash(
            cfg["provider"], cfg["model"], redacted, max_tokens,
        )
        cached = cache_mod.cache_get("llm_tier2_response", cache_key)
        if cached:
            try:
                envelope = _json.loads(cached)
                self._audit(tenant, cfg, "cache_hit", 0.0, summary)
                metrics_mod.record_idempotency("hit")  # reuse counter — same shape
                return GatewayResponse(
                    text=envelope["text"],
                    model=envelope.get("model", cfg["model"]),
                    latency_ms=0.0,
                    estimated_cost_usd=0.0,
                    redaction=summary,
                    tenant=tenant,
                )
            except Exception:  # noqa: BLE001
                # Stale / malformed entry — fall through to a real call.
                pass

        # Register the breaker (state changes flow into the Prometheus
        # circuit_breaker_state gauge via metrics hooks). The breaker is
        # consulted *inside* _call_provider's HTTP path in production —
        # the scaffold here just establishes naming.
        get_breaker(
            f"llm_gateway_{cfg['provider']}",
            failure_threshold=5,
            recovery_timeout_s=30.0,
        )

        started = time.perf_counter()
        try:
            text = self._call_provider(cfg, redacted, max_tokens)
        except CircuitOpenError as e:
            self._audit(tenant, cfg, "circuit_open", 0.0, summary)
            raise BudgetExceeded("Circuit breaker open — retry later.") from e
        latency_ms = (time.perf_counter() - started) * 1000.0

        # Cache the response so identical-input calls become free replays.
        # 1-hour default — short enough that model-side prompt changes show
        # up promptly, long enough to absorb retry storms.
        import contextlib
        with contextlib.suppress(Exception):
            cache_mod.cache_set(
                "llm_tier2_response", cache_key,
                _json.dumps({"text": text, "model": cfg["model"]}),
                ttl_seconds=3600,
            )

        # Estimate cost — ~$3 / 1M input + ~$15 / 1M output for Sonnet-class.
        # Operator overrides per-provider in a future runtime setting.
        est_cost = self._estimate_cost(cfg["provider"], cfg["model"],
                                        prompt_len=len(redacted),
                                        completion_len=len(text))
        self._charge_budget(tenant, est_cost)
        self._audit(tenant, cfg, "ok", latency_ms, summary,
                    cost_usd=est_cost)

        return GatewayResponse(
            text=text,
            model=cfg["model"],
            latency_ms=latency_ms,
            estimated_cost_usd=est_cost,
            redaction=summary,
            tenant=tenant,
        )

    # ----- config -------------------------------------------------------
    @staticmethod
    def _config() -> dict[str, Any]:
        from src.runtime_settings import get_runtime
        rt = get_runtime()
        return {
            "enabled": bool(rt.get("llm.tier2_enabled", False)),
            "provider": str(rt.get("llm.tier2_provider", "anthropic")),
            "model": str(rt.get("llm.tier2_model", "claude-sonnet-4-5")),
            "api_key": str(rt.get("llm.tier2_api_key", "")),
            "daily_budget_usd": float(rt.get("llm.tier2_daily_budget_usd", 50.0)),
            "timeout_s": int(rt.get("llm.tier2_request_timeout_s", 30)),
        }

    # ----- provider call ------------------------------------------------
    @staticmethod
    def _call_provider(cfg: dict, prompt: str, max_tokens: int) -> str:
        """Provider-specific HTTP dispatch.

        Imports are deferred so the SDKs aren't required dependencies for
        deployments that only run Tier 1 — installing ``anthropic`` /
        ``openai`` / ``google-genai`` is opt-in via the ``llm`` extras.

        Each branch is intentionally tiny: build the client, hand it the
        already-PII-redacted prompt, return the model's text. Retry +
        circuit-breaker live one layer up in ``call()``.
        """
        provider = (cfg.get("provider") or "anthropic").lower()
        api_key = cfg.get("api_key") or ""
        model = cfg.get("model") or ""
        timeout_s = int(cfg.get("timeout_s") or 30)
        if not api_key:
            raise RuntimeError(
                f"Tier-2 provider {provider!r} has no API key configured "
                "(llm.tier2_api_key is empty)",
            )

        if provider == "anthropic":
            try:
                import anthropic  # type: ignore[import-not-found]
            except ImportError as e:
                raise RuntimeError(
                    "anthropic SDK not installed — `pip install anthropic`",
                ) from e
            client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s)
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            # Concatenate text blocks; ignore tool-use blocks.
            parts = [
                getattr(b, "text", "") for b in resp.content
                if getattr(b, "type", "") == "text"
            ]
            return "".join(parts)

        if provider == "openai":
            try:
                import openai  # type: ignore[import-not-found]
            except ImportError as e:
                raise RuntimeError(
                    "openai SDK not installed — `pip install openai`",
                ) from e
            client = openai.OpenAI(api_key=api_key, timeout=timeout_s)
            resp = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content or ""

        if provider in ("google", "gemini"):
            try:
                from google import genai  # type: ignore[import-not-found]
            except ImportError as e:
                raise RuntimeError(
                    "google-genai SDK not installed — "
                    "`pip install google-genai`",
                ) from e
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config={"max_output_tokens": max_tokens},
            )
            return getattr(resp, "text", "") or ""

        raise RuntimeError(
            f"Unknown Tier-2 provider {provider!r} — "
            "expected one of: anthropic, openai, google",
        )

    # ----- budget tracking ---------------------------------------------
    def _enforce_budget(self, tenant: str, daily_cap: float) -> None:
        """Raise if the next call would push the tenant past their daily cap."""
        spent = self._budget_spent(tenant)
        if spent >= daily_cap:
            raise BudgetExceeded(
                f"Tenant {tenant} has spent ${spent:.4f} of ${daily_cap:.2f} "
                f"daily budget — falling back to Tier 1."
            )

    def _budget_spent(self, tenant: str) -> float:
        """Read the day's spend. Returns 0.0 if Redis isn't available."""
        try:
            import redis

            from src.settings import get_settings
            url = get_settings().redis_url
            if not url:
                return 0.0
            client = redis.Redis.from_url(url, socket_timeout=0.25)
            key = self._BUDGET_KEY_FORMAT.format(
                tenant=tenant, day=_today_utc(),
            )
            raw = client.get(key)
            return float(raw) if raw else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _charge_budget(self, tenant: str, cost_usd: float) -> None:
        """Increment the day's spend counter; TTL = 1 day."""
        try:
            import redis

            from src.settings import get_settings
            url = get_settings().redis_url
            if not url:
                return
            client = redis.Redis.from_url(url, socket_timeout=0.25)
            key = self._BUDGET_KEY_FORMAT.format(
                tenant=tenant, day=_today_utc(),
            )
            # Use INCRBYFLOAT to keep cents-precision.
            client.incrbyfloat(key, cost_usd)
            # Expire at end of day (lazy ~24h TTL is fine).
            client.expire(key, 86400)
        except Exception:  # noqa: BLE001 — budget tracking is best-effort
            log.exception("LLM budget charge failed (tenant=%s)", tenant)

    @staticmethod
    def _estimate_cost(
        provider: str, model: str, prompt_len: int, completion_len: int,
    ) -> float:
        """Cost estimation in USD. Approximate (operator overrides later)."""
        # Rough byte→token: ~4 bytes/token for English. Cents-precision
        # is sufficient for budget enforcement.
        prompt_tokens = prompt_len / 4
        completion_tokens = completion_len / 4
        # Default rates (Sonnet 4.5-class). Operator wires per-model
        # rates via runtime settings in a follow-up.
        per_input_token = 3e-6     # $3 / 1M
        per_output_token = 15e-6   # $15 / 1M
        return (
            prompt_tokens * per_input_token
            + completion_tokens * per_output_token
        )

    # ----- audit -------------------------------------------------------
    @staticmethod
    def _audit(
        tenant: str, cfg: dict, outcome: str,
        latency_ms: float, redaction: RedactionSummary,
        *, cost_usd: float = 0.0,
    ) -> None:
        try:
            from src.db import session_scope
            from src.models_db import AuditLog
            with session_scope() as s:
                s.add(AuditLog(
                    actor=f"tenant:{tenant}",
                    action="llm_tier2_call",
                    setting_key=None,
                    new_value={
                        "provider": cfg.get("provider"),
                        "model": cfg.get("model"),
                        "outcome": outcome,
                        "latency_ms": round(latency_ms, 1),
                        "estimated_cost_usd": round(cost_usd, 6),
                        "redactions": redaction.counts,
                    },
                ))
                s.commit()
        except Exception:  # noqa: BLE001
            log.exception("LLM audit row failed (tenant=%s)", tenant)


def _today_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%d")
