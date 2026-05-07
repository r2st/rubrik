# ADR 0003: Self-Host Summarization with Gemma 4 over Vendor APIs

- **Status:** Accepted
- **Date:** 2026-05-05

## Context

The pipeline today consumes pre-computed summaries from `summary.json`. In production, those summaries need to come from somewhere. Two architectures:

1. **Vendor API** (GPT-4-class). API key + prompt; ready immediately.
2. **Self-hosted open model**. Train + serve our own; no data egress.

This decision affects every customer transcript that flows through the system, indefinitely.

### What we tried

The client provided a representative *sample* of meeting transcripts and confirmed synthetic generation is acceptable for edge cases the sample doesn't cover. As a proof-of-concept that the **recipe** (fine-tune Gemma 4 on the dataset's gold summaries) works, we fine-tuned **Gemma 4** (E2B and E4B variants) on the sample's 95 train + 5 held-out meetings using QLoRA on a Nebius H100. Four iterations:

| Run | Base | LoRA | Epochs | Train loss | Val loss | ROUGE-L | Notes |
|---|---|---|---|---|---|---|---|
| v1 | E2B | r4 / α8 | 3 | 1.73 | — | — | Pipeline validation |
| v2 | E2B | r16 / α32 | 5 | 1.18 | — | — | More capacity |
| **v3 ★** | **E4B** | **r16 / α32** | **3** | **0.37** | **1.01** | **0.394** | **Recommended** |
| v4 | E4B | r16 / α32 + dropout | 2 | 0.40 | 1.52 | 0.337 | Over-regularized |

**Headline:** baseline E4B ROUGE-L = 0.286 → tuned v3 = **0.394 (+38% relative)**, all 5 held-out outputs visibly shifted to match reference style.

**Total cost:** $1.40 on Nebius on-demand, ~28 minutes wall-clock across all 4 runs.

### Decision matrix

| Dimension | Vendor API | Self-hosted Gemma 4 |
|---|---|---|
| Setup cost | API key + prompt | One workshop, four iterations |
| Per-call cost | $0.005–$0.02 / transcript | ~$0 (GPU amortization) |
| One-time training | $0 | $1.40 |
| Latency | 1–3s | 50–200ms (GPU) |
| Determinism | Vendor versioning kills it | Bit-exact with greedy + pinned checkpoint |
| Privacy | Customer data leaves perimeter | Self-hostable, deployable in customer VPCs |
| Output format control | Prompt engineering | Trained into weights |
| Style match | Generic | Reference voice locked in |

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
- The proof-of-concept run trained on the client's sample (95 meetings), which is well below the comfortable training floor for production deployment. The path forward is twofold: (a) the active-learning loop in ADR 0010 generates labels from real production traffic, and (b) synthetic transcript generation (Claude / GPT-4) covers edge cases the natural traffic stream is slow to surface
- ROUGE-L is a lexical metric and penalizes paraphrase; LLM-as-judge is the better next metric (code already in `gemma-finetune/code/judge_compare.py`)

### Scale envelope

| Stage | At sample volume (today) | At production volume (millions+) |
|---|---|---|
| Training | Single H100, 28 min, $1.40 | Multi-node FSDP via Ray Train (ADR 0010), spot H100 pool |
| Adapter size | 30 MB | Same (LoRA scales by rank, not dataset size) |
| Inference | Single L4 pod = ~10 RPS | Autoscaled vLLM with multi-LoRA hot-swap (ADR 0010) |
| Active learning | Manual eval | Continuous label generation from production traffic |

**Neutral**
- This decision is *coupled* to ADR 0002 (categorization stays rules-based). Rules + small fine-tuned LLM is the right division of labor: rules for the head, LLM for the long tail of free-text generation.

## When to revisit

- A vendor API gets cheaper than amortized GPU cost (currently breakeven ~2k transcripts/day)
- Per-customer fine-tuning becomes operationally infeasible — fall back to vendor API or shared model
- A new generation of open models (Gemma 5, Llama 4) materially raises the quality ceiling

## Related

- `gemma-finetune/README.md` — full methodology, 4 training iterations, implementation lessons
- `docs/APPROACH.md` § 2 — comparison matrix and verdict in narrative form
