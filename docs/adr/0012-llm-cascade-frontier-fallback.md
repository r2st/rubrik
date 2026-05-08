# ADR 0012: LLM Cascade — Self-Hosted Fine-Tune for Bulk, Frontier API for Edge Cases

- **Status:** Proposed
- **Date:** 2026-05-07

## Context

ADR 0003 committed us to a self-hosted **Gemma 4 + QLoRA** fine-tune as the LLM tier — summaries and action-item extraction in the client's house style, served via vLLM with multi-tenant LoRA hot-swap (ADR 0010). The recommended adapter (`v3-e4b-allrec`) hits ROUGE-L 0.39 on held-out summaries at ~150 ms / call on H100. For the **bulk of in-distribution work, this is the right tool.**

It is the wrong tool for several real production patterns:

1. **Out-of-distribution meetings** — a new product line, a new language, a regulatory-heavy domain not in the training set. The fine-tune generates plausible but off-target output and we have no easy way to detect it from the output alone.
2. **Long-context reasoning** — quarterly account reviews, multi-meeting root-cause synthesis, full-history briefings. Gemma's effective context is ~8k tokens; frontier models are at 200k–2M.
3. **World-knowledge-dependent tasks** — comparing the customer's setup to a competitor product released after the training cutoff, or reasoning over recently-changed industry regulations.
4. **High-stakes outputs** — executive briefings, customer-facing letters, legal-escalation summaries. Volume is low; cost-per-call doesn't matter; quality ceiling does.
5. **Complex chain-of-thought** — root-cause synthesis, contradictory-evidence reconciliation. 4B parameters has a real reasoning ceiling.

We need a way to route those workloads to a stronger model **without** blowing up the cost economics of the bulk path or creating a hard third-party dependency.

## Decision

We add a **Tier 2 frontier-LLM gateway** in front of an external API (Claude / GPT-4 / Gemini Pro — chosen at runtime via the admin panel) and route to it from Tier 1 when escalation triggers fire.

**Tier 1 (default, self-hosted):** Gemma 4 + QLoRA via vLLM. Handles ~95% of generative traffic.

**Tier 2 (escalation, external API):** Frontier model via a thin gateway that enforces:

| Guardrail | Why |
|---|---|
| **PII redaction** (regex + spaCy NER) before any payload leaves the perimeter | Customer data must not exit without scrubbing. |
| **Per-tenant policy** | Tenants requiring data residency or "no third-party LLM" are blocked at the hop and get the Tier-1 result flagged `low_confidence=true`. |
| **Per-tenant daily $ cap** | Hard budget — over cap returns the Tier-1 result and alerts the operator. |
| **Response cache** keyed on `(input_hash, prompt_version, model_id)` | Identical inputs don't re-pay. |
| **Audit-log entry** per call (model, latency, cost, redaction summary, tenant) | Compliance + debugging + cost attribution. |

**Escalation triggers** (any one → Tier 2):

| Trigger | Source signal |
|---|---|
| Generation perplexity > threshold | Tier-1 token logits during decode |
| LLM-as-judge score < threshold | Existing active-learning judge pass on Tier-1 output |
| Out-of-distribution input | Input-embedding distance to training centroid > threshold |
| Operator/product flag | `runtime_settings` per category (e.g., `llm.tier2_categories = ["legal", "exec_brief"]`) |
| Long context | Input > 8k tokens or multi-meeting joint analysis |

**Active-learning loop closes around Tier 2.** Every escalation produces a `(production input, frontier reference output)` pair that flows into the queue per ADR 0010. Each fine-tune iteration should drive Tier-2 traffic share down — that's the headline metric for the loop.

## Rationale

The cost math is the load-bearing argument:

| Model | Cost / 1k summaries | Latency / call | Quality on the trained tasks |
|---|---|---|---|
| Tier 1 (Gemma E4B + LoRA on H100) | ~$0.10 amortized | ~150 ms | ROUGE-L 0.394 |
| Tier 2 (frontier API, ~2k input + 500 output tokens) | $5–$50 | 1–3 s | Higher on edge cases; comparable on in-distribution |

At 100M meetings/year (the architecture target):
- **Frontier-only** = $500k–$5M/year + ~3 s p95 + a hard external dependency on a critical path.
- **Cascade @ 5% Tier 2** = ~$25k–$250k/year + ~150 ms p95 (Tier 2 is async / low-volume) + graceful degradation to Tier 1 if the external API is down.

The cascade is the only design that gives us **frontier-quality on the cases that need it** *and* **self-hosted economics on the cases that don't**.

## Alternatives considered

| Option | Verdict |
|---|---|
| **Frontier-only.** No fine-tune, just call Claude/GPT-4 on every meeting. | Rejected. Cost, latency, third-party dependency on the hot path. |
| **Fine-tune-only.** Ship the cascade later. | Rejected for production. Edge-case quality drops silently and we have no detection layer. Acceptable for the current development state — Tier 2 is documented but not yet shipped. |
| **Bigger fine-tune (E27B).** Train a larger Gemma to absorb the edge cases. | Rejected as a *replacement*. The training data we'd need to cover the edge cases doesn't exist yet — that's the active-learning loop's job. A larger model amplifies the problem we don't yet have the data for. We may revisit when active-learning has produced enough Tier-2 samples to justify it. |
| **Multiple specialized fine-tunes** (one per product/language). | Defer. Multi-tenant LoRA hot-swap (ADR 0010) makes this cheap when needed, but it still doesn't solve world-knowledge or long-context cases. |

## Consequences

**Positive**
- Quality ceiling matches frontier models on the cases that need it.
- Bulk economics still self-hosted.
- Active-learning loop now has a high-signal source: every Tier-2 call is training data the fine-tune lacked.
- Cost is bounded per tenant (hard $ cap) and predictable (cache + budget alarms).
- Privacy posture is explicit: tenants opt in or out of external LLM use, redaction is mandatory, and policy is auditable.

**Negative**
- New external dependency on the chosen frontier API (Claude / OpenAI / Google). Mitigated by the Tier-1 fallback when Tier 2 fails or is over budget.
- New code surface: confidence-scoring, OOD detection, the gateway, redaction, caching, budgets. Realistic estimate: ~600 LOC + tests, plus runtime-settings keys and an admin UI section.
- Cost forecasting requires telemetry we don't yet emit (per-tenant Tier-2 share by category). Adds a Prometheus dimension.
- Adversarial inputs designed to **force** Tier-2 escalation are a new attack vector against the $ budget — guard with rate-limit on confidence-trigger frequency per tenant.

**Neutral**
- The Tier-2 model choice is itself runtime-tunable (Claude vs GPT-4 vs Gemini) so we can A/B and switch on price/quality movement without a deploy.

## When to revisit

- Tier-2 traffic share for a category drops below ~1% sustained → the fine-tune has absorbed the pattern; consider lowering the trigger threshold to recover budget headroom or raising it to harvest more training data.
- A new use case lands that Tier 1 can't cover and that doesn't fit the trigger taxonomy → redesign trigger logic before adding ad-hoc category flags.
- A frontier model's price drops 10× (or open-weights catches up) → revisit the cascade-vs-single-model tradeoff. The cascade is an answer to current price/quality math, not a permanent commitment.
- Compliance regime shifts (e.g., HIPAA-bound tenant) → may force "Tier 1 only" tenants permanently; the gateway already supports this via per-tenant policy.

## Implementation plan (when prioritized)

1. **Confidence layer** in Tier 1 — perplexity + judge score emitted per call (instrumentation only; no behavior change).
2. **Gateway service** — thin FastAPI service in front of one frontier provider; redaction + cache + budget.
3. **Routing** in `api/state.py` / generation path — escalate when triggers fire; fall back to Tier-1 result on gateway failure.
4. **Admin panel additions** — per-tenant policy, budgets, model choice, trigger thresholds — all under the `llm.*` runtime-settings namespace. **Shipped:** seven keys under category `llm` (`llm.tier1_endpoint`, `llm.tier2_enabled`, `llm.tier2_provider`, `llm.tier2_model`, `llm.tier2_api_key` — the masked-on-read `secret` type — `llm.tier2_daily_budget_usd`, `llm.tier2_request_timeout_s`). The API key uses the new `secret` type so it's masked in `GET /admin/settings`, masked in audit-log rows, and never leaves the DB in raw form. Per-tenant policy / per-category trigger flags follow the gateway service.
5. **Telemetry** — Prometheus metric `llm_tier2_share{tenant, category}`; Grafana dashboard tracks the active-learning loop's headline number.
6. **Active-learning wiring** — Tier-2 reference outputs land in the existing queue; nightly batch builds a candidate dataset for the next fine-tune.

## Related

- ADR 0002 — Hybrid categorization (rules + classical ML; **no LLM in categorization**)
- ADR 0003 — Self-host summarization with Gemma 4 (the Tier-1 choice)
- ADR 0009 — Admin panel for runtime config (per-tenant policy + budgets live here)
- ADR 0010 — Auto-scaling ML pipeline (active-learning loop closes around Tier 2)
- `gemma-finetune/` — current Tier-1 adapters
- `docs/ARCHITECTURE.md` § "LLM cascade — fine-tuned for bulk, frontier model for edge cases"
