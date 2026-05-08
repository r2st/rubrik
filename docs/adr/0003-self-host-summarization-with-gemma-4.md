# ADR 0003: Self-Host Summarization with Gemma 4 over Vendor APIs

- **Status:** Accepted
- **Date:** 2026-05-05

## Context

The pipeline today consumes pre-computed summaries from `summary.json`. In production, those summaries need to come from somewhere. Two architectures:

1. **Vendor API** (GPT-4-class). API key + prompt; ready immediately.
2. **Self-hosted open model**. Train + serve our own; no data egress.

This decision affects every customer transcript that flows through the system, indefinitely.

### What we tried

A QLoRA fine-tune of Gemma 4 was completed during development to validate the recipe and the cost economics; production runs the same trainer on a multi-node cluster (ADR 0010). We fine-tuned **Gemma 4** (E2B and E4B variants) using QLoRA on a single Nebius H100. Four iterations:

| Run | Base | LoRA | Epochs | Train loss | Val loss | ROUGE-L | Notes |
|---|---|---|---|---|---|---|---|
| v1 | E2B | r4 / α8 | 3 | 1.73 | — | — | Pipeline validation |
| v2 | E2B | r16 / α32 | 5 | 1.18 | — | — | More capacity |
| **v3 ★** | **E4B** | **r16 / α32** | **3** | **0.37** | **1.01** | **0.394** | **Recommended** |
| v4 | E4B | r16 / α32 + dropout | 2 | 0.40 | 1.52 | 0.337 | Over-regularized |

**Headline:** baseline E4B ROUGE-L = 0.286 → tuned v3 = **0.394 (+38% relative)**, held-out outputs visibly shifted to match reference style.

**Development-time cost (for the recipe validation only):** ~$1.40 on Nebius on-demand, ~28 minutes wall-clock across all 4 runs. These figures characterize the development environment; they are not assertions about a production training run.

### Decision matrix — vendor API vs self-host

| Dimension | Vendor API | Self-hosted Gemma 4 |
|---|---|---|
| Setup cost | API key + prompt | Trainer + four iterations |
| Per-call cost | $0.005–$0.02 / transcript | ~$0 (GPU amortization) |
| One-time training | $0 | Development-time QLoRA recipe (validated) |
| Latency | 1–3 s | 50–200 ms (GPU) |
| Determinism | Vendor versioning kills it | Bit-exact with greedy + pinned checkpoint |
| Privacy | Customer data leaves perimeter | Self-hostable, deployable in customer VPCs |
| Output format control | Prompt engineering | Trained into weights |
| Style match | Generic | Reference voice locked in |

### Decision matrix — Gemma 4 vs other open models

Within the self-host lane, the open-model field is crowded. Why Gemma 4 specifically:

| Family | Size(s) considered | License | Quality on our task* | Notes / why not picked |
|---|---|---|---|---|
| **Gemma 4** ★ | E2B, **E4B** | Gemma Terms (commercial OK with notice) | **+38% ROUGE-L over baseline** after QLoRA on a small dev corpus | Picked. See "Why Gemma 4 specifically" below. |
| **Llama 3.2** | 1B, 3B, 8B, 70B, 405B | Llama Community (free ≤ 700M MAU) | Small variants underperform Gemma 4 E4B on our held-out summary eval; large variants miss our latency / single-pod inference budget | Strong dense models, big community. Smaller variants (1B / 3B) lose on quality; larger variants (70B+) lose on serving cost. |
| **Qwen 2.5** | 0.5B – 72B | Apache 2.0 (most sizes) | Comparable to Gemma 4 at 7B; slightly behind on style-match | Best license. Lost on **native multimodal** (we want video/screenshot capability on the roadmap). Strong fallback if Gemma's license becomes a blocker. |
| **Mistral / Mixtral** | 7B, 8×7B, 8×22B | Apache 2.0 | Mistral 7B comparable; Mixtral overkill | Mixtral's MoE structure is suited to broader generalist tasks; for narrow-domain summarization the operator footprint isn't worth it. Mistral 7B is dense at a size where Gemma E4B's sparse-effective design wins on inference cost. |
| **Phi-3** | 3.8B mini, 14B | MIT | Strong on reasoning; weaker on long-form summarization style match | Excellent reasoning benchmarks. Style transfer to our reference voice was less convincing in the eval. |
| **DeepSeek V2 / V3** | 16B-effective, 671B | DeepSeek License | Excellent reasoning | Total VRAM requirements (even with MoE) overshoot a single-H100 fine-tune budget. Operator footprint heavier than we want for the bulk path. The Tier-2 cascade (ADR 0012) is where you'd reach for DeepSeek-class reasoning. |

\* "Quality on our task" = summarization-faithfulness + bulleted-action-item extraction in the reference voice, measured during recipe validation. Numbers are directional; we don't claim a public leaderboard ranking.

#### Why Gemma 4 specifically (the five reasons)

1. **Sparse-effective compute (E-series).** E2B and E4B run at compute parity with their effective size while quality tracks much larger dense models. We sit on a good point of the cost/quality curve for self-hosting.
2. **Native multimodal.** E4B accepts image input. We don't use it today, but screen-share + slide-deck context is a roadmap item; switching families later costs more than buying the optionality up front.
3. **Tooling maturity.** First-class support in `unsloth` (the QLoRA pipeline we use), `vLLM` (the serving stack ADR 0010 commits to), `transformers`, and PEFT. Not every alternative ships that combination cleanly.
4. **Memory profile.** QLoRA on E4B fits comfortably on a single H100 — recipe validation hit ~28 min, ~$1.40 wall-clock. Llama 70B doesn't; Qwen 32B is borderline.
5. **Style transfer worked.** v3 hit ROUGE-L 0.394 (+38% over the untuned E4B baseline of 0.286) and held-out outputs visibly matched the reference voice. The recipe transfers.

#### Honest tradeoffs

- **License is more restrictive than Apache 2.0.** Tolerable for the current use; flagged in "When to revisit" below.
- **Smaller community** than Llama — fewer pre-trained domain adapters as starting points.
- **Newer family** → less battle-tested at production scale.

If any of those tradeoffs flip, the swap is genuinely a one-file change: replace the `base_model` constant in `gemma-finetune/code/finetune_v3.py`, re-run the same recipe. The rest of the pipeline is model-agnostic by design.

## Decision

We **adopt the v3 Gemma 4 adapter for production summarization**. Vendor APIs are a stopgap during early onboarding only.

- Adapter checkpoint: `gemma-finetune/adapters/v3-e4b-allrec/attendee-12-v3-r1.adapter/` (weights gitignored, reproducible from `gemma-finetune/code/finetune_v3.py`)
- Inference path: load via `unsloth.FastModel.from_pretrained()` — resolves base + LoRA in one call
- Lives behind the same FastAPI service; sync invocation is fine (50–200ms)

## Consequences

**Positive**
- No customer data leaves the perimeter — major win for regulated-industry customers
- Per-call cost drops to GPU amortization; breakeven against vendor APIs is ~2k transcripts/day
- Output format is locked in (paragraph + `Owner: task` bullets) — no prompt engineering drift
- Determinism is restored: pinned checkpoint + greedy decode = reproducible outputs for audit

**Negative**
- MLOps overhead: versioned models, retraining cadence, drift monitoring, evaluation harness
- The development-time recipe-validation run trained on a small corpus, well below the comfortable training floor for production deployment. The path forward is twofold: (a) the active-learning loop in ADR 0010 generates labels from real production traffic, and (b) synthetic transcript generation (Claude / GPT-4) covers edge cases the natural traffic stream is slow to surface
- ROUGE-L is a lexical metric and penalizes paraphrase; LLM-as-judge is the better next metric (code already in `gemma-finetune/code/judge_compare.py`)

### Scale envelope

| Stage | Development (recipe validation) | At production volume (millions+) |
|---|---|---|
| Training | Single H100 | Multi-node FSDP via Ray Train (ADR 0010), spot H100 pool |
| Adapter size | 30 MB | Same (LoRA scales by rank, not dataset size) |
| Inference | Single L4 pod = ~10 RPS | Autoscaled vLLM with multi-LoRA hot-swap (ADR 0010) |
| Active learning | Manual eval | Continuous label generation from production traffic |

**Neutral**
- This decision is *coupled* to ADR 0002 (categorization stays rules-based). Rules + small fine-tuned LLM is the right division of labor: rules for the head, LLM for the long tail of free-text generation.

## When to revisit

- A vendor API gets cheaper than amortized GPU cost (currently breakeven ~2k transcripts/day).
- Per-customer fine-tuning becomes operationally infeasible — fall back to vendor API or shared model.
- A new generation of open models (Gemma 5, Llama 4) materially raises the quality ceiling.
- **Gemma's license becomes a blocker** (e.g., a customer's procurement requires Apache 2.0). Swap to **Qwen 2.5 7B** is the documented fallback — same recipe, one constant change in the trainer.
- A frontier model reaches "Tier 1 quality at Tier 2 economics" — re-evaluate the cascade in ADR 0012.

## Related

- `gemma-finetune/README.md` — full methodology, 4 training iterations, implementation lessons
- `docs/APPROACH.md` § 2 — comparison matrix and verdict in narrative form
