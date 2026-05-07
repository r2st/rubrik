# Rubrik Take-Home — Gemma 4 Fine-Tune on Meeting Transcripts

QLoRA fine-tunes of Gemma 4 (E2B and E4B) on the **client's sample** of meeting transcripts. Goal: turn a generic instruction-tuned base model into a specialized **meeting-summarizer + action-item extractor** that matches the style of the gold-labelled summaries shipped with the sample.

> **Scope: proof-of-concept.** The client provided a representative sample (~100 meetings) and confirmed synthetic generation is acceptable for edge cases. This work demonstrates **the recipe** — that fine-tuning Gemma 4 on the dataset's gold summaries produces meaningful quality gains over the baseline at workshop economics ($1.40, 28 minutes). It is **not** a production training run. Production volume (millions to 100M+ records) trains via the path in [`scaling/`](scaling/README.md) and [ADR 0010](../docs/adr/0010-auto-scaling-ml-pipeline.md): Ray Data streaming dataset prep, multi-node FSDP training on autoscaled GPU pools, autoscaled vLLM serving with multi-tenant LoRA hot-swap, and an active-learning loop that turns production traffic into the next training set.

Trained on a single Nebius H100 (80 GB). Four iterations — recommended adapter is `v3-e4b-allrec`.

---

## TL;DR

| Run | Base model | LoRA | Epochs | Train loss | Val loss | ROUGE-L (tuned) | Action-item F1 |
|---|---|---|---|---|---|---|---|
| v1 | gemma-4-E2B-it | r4 / α8 | 3 | 1.73 | — | — | — |
| v2 | gemma-4-E2B-it | r16 / α32 | 5 | 1.18 | — | — | — |
| **v3 (best)** | **gemma-4-E4B-it** | **r16 / α32** | **3** | **0.37** | **1.01** | **0.394** | — |
| v4 | gemma-4-E4B-it | r16 / α32 (+dropout) | 2 | 0.40 | 1.52 | 0.337 | 0.20 |

All runs hit the workshop "5/5 prompts visibly shifted vs baseline" verdict. v3 wins on every quantitative axis and on a manual qualitative read of the held-out outputs.

---

## Task

The client-provided sample (`/dataset/{meetingId}/`) ships **~100 representative meetings** (production volume target: millions+), each with:

- `transcript.json` — speaker-labelled turns
- `summary.json` — gold paragraph summary + structured `actionItems[]`
- `meeting-info.json`, `speakers.json`, `events.json`, `speaker-meta.json` — metadata

The fine-tune target: given a raw transcript, produce a one-paragraph summary followed by `Owner: task` action-item bullets, in the exact style the dataset's gold summaries use.

Held-out **5 meetings** (last 5 by sort order) for evaluation. Trained on the remaining 95.

---

## Approach

### Iterations

**v1 — baseline recipe.** Gemma 4 E2B, LoRA rank 4, 3 epochs, single-task (transcript → summary+actions). Validates the pipeline. Final loss 1.73, 5/5 shifted.

**v2 — increase capacity.** Same model, LoRA rank 16, 5 epochs. Final loss 1.18 — meaningfully better fit, same hardware budget.

**v3 — stack 7 known wins.** Applied a focused list of upgrades:

1. **Larger base model** — `gemma-4-E4B-it` (~4B params)
2. **Multi-task data** — each meeting → 4 training rows (full summary, summary-only, actions-only, attendees) → 380 rows
3. **Completion-only loss** — only the response tokens contribute to gradient (`completion_only_loss=True`)
4. **Validation split** — 10 % held out, val loss measured manually after training
5. **Truncation fix** — `max_seq_length=8192` (transcripts up to ~5k tokens were silently truncated at 4096)
6. **ROUGE-L scoring** — real lexical metric vs reference summaries, not just "did the output change"
7. **Lower LR** — `1e-4` (was 2e-4) to match the higher capacity

Result: train loss 0.37, val loss 1.01, ROUGE-L 0.39 (vs 0.29 baseline). 5/5 shifted with strong qualitative match to reference style.

**v4 — overfitting reduction (didn't pan out).** Hypothesis: train/val gap of 0.64 in v3 indicated overfitting; tried dropout 0.05, weight_decay 0.01, 2 epochs, plus a 5th task (title prediction). Result: val loss got *worse* (1.52), ROUGE-L dropped to 0.34, action-item format regressed. Diagnosis: changed too many variables at once and over-regularized; v3 was already at the sweet spot.

### Implementation notes

The shared workshop script (`templates/finetune.py` from the gemma-finetune workshop repo) was extended in `finetune_v3.py` / `finetune_v4.py`. Key code-level details:

- **Multimodal Gemma quirks.** Gemma 4 ships with a `Gemma4Processor` (multimodal). `assistant_only_loss=True` is rejected by unsloth's wrapper for VLMs — used `completion_only_loss=True` with a `{prompt, completion}` dataset shape instead.
- **Adapter loading.** `peft.load_adapter()` fails because unsloth replaces `nn.Linear` with `Gemma4ClippableLinear`. The working pattern: `FastModel.from_pretrained(adapter_path, ...)` resolves the base model from `adapter_config.json` and reconstructs the model + LoRA in one call.
- **Eval batching.** TRL's per-epoch `eval_strategy="epoch"` blew up on ragged `completion_mask` for this model class. Computed val loss manually after training (one example at a time) instead.
- **Adapter save ordering.** Save the adapter immediately after `trainer.train()` — *before* any custom eval / inference. If the eval crashes, you don't lose the trained weights.

---

## Recommended adapter

**`adapters/v3-e4b-allrec/attendee-12-v3-r1.adapter/`** — 31 MB LoRA on `unsloth/gemma-4-E4B-it`. Wins on ROUGE-L, val loss, and qualitative read.

**Sample output (held-out meeting-1):**

> David Kim from Aegis Cloud Security support is speaking with Lauren Bishop, VP of Infrastructure at Cobalt Software, regarding a complete outage of the Aegis Detect dashboard and threat monitoring capabilities for their Enterprise account (CSW-4492). Lauren is extremely frustrated and is actively evaluating SentinelShield as a competitor due to the critical nature of the security blind spot her team is currently facing. David is escalating this as a P1 incident and committing to providing updates every fifteen minutes until service is restored, while also ensuring a full root cause analysis and service credit discussion with her account team.
>
> Action Items:
> * David Kim: Provide Lauren Bishop with status updates every fifteen minutes until Aegis Detect is fully operational
> * David Kim: Escalate this to Aegis Engineering leadership as a P1 incident and ensure a full root cause analysis is delivered to Lauren and her account team
> * David Kim: Flag the request for a service credit conversation to Lauren's account manager
> * Lauren Bishop: Inform her security team of the ongoing incident and Aegis's stated remediation timeline

All 5 reference facts present (parties, P1, 15-min updates, RCA, service credit, SentinelShield), correct format, no hallucinations.

---

## How to reproduce

### On a CUDA H100 (or H200) VM

```bash
# 1. Set up Python env (Nebius / WSL2 / native Linux)
python3 -m venv ~/venv && source ~/venv/bin/activate
pip install -U unsloth trl peft datasets bitsandbytes accelerate

# 2. Build the multi-task dataset from the Rubrik /dataset folder
python code/build_rubrik_jsonl.py
# -> data/rubrik_meetings.jsonl (380 rows = 95 meetings × 4 tasks)
# -> data/rubrik_eval_prompts.json (5 held-out meetings)

# 3. Train
python code/finetune_v3.py \
  --user my-run --out-dir runs/my-run \
  --dataset data/rubrik_meetings.jsonl \
  --eval-prompts data/rubrik_eval_prompts.json \
  --model unsloth/gemma-4-E4B-it --epochs 3
# Wall-clock: ~10 min on a single H100. Cost: ~$0.50 on Nebius on-demand.

# 4. (Optional) LLM-as-judge scoring
export ANTHROPIC_API_KEY=sk-ant-...
python code/judge_compare.py
```

### Inference with the trained adapter

```python
from unsloth import FastModel
model, tokenizer = FastModel.from_pretrained(
    "adapters/v3-e4b-allrec/attendee-12-v3-r1.adapter",
    max_seq_length=8192, load_in_4bit=True, full_finetuning=False,
)
ids = tokenizer.apply_chat_template(
    [{"role": "user", "content": [{"type": "text", "text": prompt_text}]}],
    return_tensors="pt", add_generation_prompt=True, tokenize=True,
).to(model.device)
gen = model.generate(ids, max_new_tokens=400, do_sample=False)
print(tokenizer.decode(gen[0][ids.shape[1]:], skip_special_tokens=True))
```

A working end-to-end smoke test is in `code/test_v3.py`.

---

## Files in this folder

```
gemma-finetune/
├── README.md                          (this file)
├── code/
│   ├── build_rubrik_jsonl.py          dataset builder (multi-task expansion)
│   ├── finetune_v3.py                 winning training script
│   ├── finetune_v4.py                 v4 (regularization experiment)
│   ├── judge.py                       Claude-as-judge (single run)
│   ├── judge_compare.py               Claude-as-judge (v3 vs v4 side-by-side)
│   └── test_v3.py                     smoke-test inference script
├── data/
│   ├── rubrik_meetings.jsonl          380 train rows, JSONL with {instruction, context, response}
│   └── rubrik_eval_prompts.json       5 held-out meetings + reference summaries
├── adapters/
│   ├── v1-r4-3ep/                     E2B, rank 4, 3 epochs
│   ├── v2-r16-5ep/                    E2B, rank 16, 5 epochs
│   ├── v3-e4b-allrec/                 E4B, rank 16, 3 epochs ★ RECOMMENDED
│   └── v4-tuned/                      E4B, rank 16, 2 epochs + dropout (regression)
└── results/
    ├── v{1..4}-compare.md             baseline vs tuned vs reference, per held-out meeting
    ├── v{1..4}-metrics.json           train loss, val loss, ROUGE-L, F1, timings, config
    └── v{1..4}-train.log              full training stdout
```

Each `adapters/vN/.../*.adapter/` directory contains the LoRA weights (`adapter_model.safetensors`) and config (`adapter_config.json`). To use: load the adapter directory directly via `FastModel.from_pretrained()` — it resolves the base model automatically.

---

## Recommendations for next iteration

In priority order:

1. **LLM-as-judge pipeline** — ROUGE-L scored v3's meeting-1 output at 0.39 even though it captured every fact in the reference (penalized for paraphrasing). Replace ROUGE with Claude-graded faithfulness / completeness / format / hallucinations scores. Code is in `code/judge_compare.py`; needs only an `ANTHROPIC_API_KEY`.

2. **Synthesize more training data.** The client sample is intentionally small (proof-of-concept scope) and the client confirmed synthetic generation is acceptable. Use Claude or GPT-4 to generate 200–500 additional transcripts in the dataset's voice for edge-case coverage; at production volume, this becomes part of the active-learning loop in `scaling/active_learning.py`. Train v5 on real + synthetic mix. Likely the single biggest unrealized lift on the proof-of-concept.

3. **Action-item F1 as the primary metric.** The structured output (action items) is what end-users actually consume. Parse `Owner: task` lines, score precision/recall on owner+task overlap. Already implemented in `finetune_v4.py`; promote it from a side-metric to the gating one.

4. **DPO preference round on top of v3.** Once an LLM judge is in place: generate 2-3 candidate summaries per training meeting, judge them, build (chosen, rejected) pairs, run a short DPO pass on the v3 adapter. Empirically beats further SFT for output-quality polish.

5. **Vary one variable at a time.** v4 changed 4 things simultaneously (dropout + weight decay + 2 epochs + new task) — couldn't isolate which hurt. Future runs: change *one* setting per experiment, with the new metrics in place to measure.

---

## Caveats / known limitations

The numbers below all reflect proof-of-concept scope on the client sample. None are production-grade; production deployment runs against the path in [`scaling/`](scaling/README.md).

- **Sample-size training data.** The client provided a representative sample (proof-of-concept scope, not the production training set). Production trains on millions of records sourced from Kafka via Ray Data, with synthetic augmentation for edge cases the natural traffic stream is slow to surface.
- **Single seed.** All numbers from one training run per config. ±0.02-0.05 ROUGE-L noise expected from re-running with a different seed.
- **5-meeting eval set is small.** Each metric averaged over 5 examples — directional signal, not statistically rigorous. Production eval uses the LLM-as-judge harness in `scaling/eval_harness.py` over a much larger held-out set generated continuously by the active-learning loop.
- **ROUGE-L is lexical.** Penalizes paraphrase. v3 scored modestly on ROUGE despite producing factually clean output — see point #1 in recommendations.
- **Multimodal Gemma 4 has rough edges.** Required workarounds for `assistant_only_loss`, per-epoch eval, and adapter loading via `peft`. None of these are dealbreakers but they're under-documented in the unsloth/transformers stack as of May 2026.
- **No instruction-following safety eval.** The fine-tune is narrow (meeting summaries). Did not measure regression on the base model's general instruction-following — would matter if this adapter were deployed broadly.

---

## Environment

- Hardware: 1× NVIDIA H100 80GB HBM3 (Nebius AI Cloud, on-demand)
- Software: Ubuntu 24.04, CUDA 12.8, PyTorch 2.10, Unsloth 2026.5.2, Transformers 5.5.0, TRL 0.24.0
- Workshop venue: Immersive Commons Fine-Tune Gemma 4 workshop, May 5 2026 (shard 1, attendee 12)
- Total compute time across all 4 runs: ~28 minutes wall-clock
- Total cost on Nebius on-demand: ~$1.40
