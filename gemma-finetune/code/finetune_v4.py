"""v3: completion-only loss, val split, 8k seq, ROUGE-L scoring."""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path


def load_eval_prompts(path):
    raw = json.loads(path.read_text())
    return raw["prompts"] if isinstance(raw, dict) and "prompts" in raw else raw


def run_inference(model, tokenizer, prompts, max_new_tokens):
    out = {}
    for p in prompts:
        ids = tokenizer.apply_chat_template(
            [{"role": "user", "content": [{"type": "text", "text": p["prompt"]}]}],
            return_tensors="pt", add_generation_prompt=True, tokenize=True,
        ).to(model.device)
        gen = model.generate(ids, max_new_tokens=max_new_tokens, do_sample=False)
        out[p["id"]] = tokenizer.decode(gen[0][ids.shape[1]:], skip_special_tokens=True).strip()
    return out


def lcs_len(a, b):
    n, m = len(a), len(b)
    if n == 0 or m == 0: return 0
    dp = [0] * (m + 1)
    for i in range(1, n + 1):
        prev = 0
        for j in range(1, m + 1):
            tmp = dp[j]
            dp[j] = prev + 1 if a[i-1] == b[j-1] else max(dp[j], dp[j-1])
            prev = tmp
    return dp[m]


def rouge_l(pred, ref):
    p_tok = pred.lower().split()
    r_tok = ref.lower().split()
    if not p_tok or not r_tok: return 0.0
    lcs = lcs_len(p_tok, r_tok)
    prec = lcs / len(p_tok)
    rec = lcs / len(r_tok)
    if prec + rec == 0: return 0.0
    return 2 * prec * rec / (prec + rec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--model", default="unsloth/gemma-4-E4B-it")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--eval-prompts", required=True)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--lora-dropout", type=float, default=0.05)
    ap.add_argument("--weight-decay", type=float, default=0.01)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--max-seq-length", type=int, default=8192)
    ap.add_argument("--max-eval-tokens", type=int, default=400)
    ap.add_argument("--val-frac", type=float, default=0.10)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timings = {}

    eval_prompts = load_eval_prompts(Path(args.eval_prompts))
    print(f"[eval] {len(eval_prompts)} held-out prompts")

    print(f"[model] loading {args.model}...")
    t0 = time.time()
    from unsloth import FastModel
    model, tokenizer = FastModel.from_pretrained(
        args.model, max_seq_length=args.max_seq_length,
        load_in_4bit=True, full_finetuning=False,
    )
    timings["model_load_s"] = round(time.time() - t0, 2)

    # Baseline
    t0 = time.time()
    base_out = run_inference(model, tokenizer, eval_prompts, args.max_eval_tokens)
    timings["baseline_s"] = round(time.time() - t0, 2)

    # LoRA
    model = FastModel.get_peft_model(
        model, r=args.rank, lora_alpha=args.alpha,
        lora_dropout=args.lora_dropout,
        target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
    )

    # Dataset + train/val split
    from datasets import load_dataset
    from trl import SFTTrainer, SFTConfig

    ds = load_dataset("json", data_files=args.dataset, split="train")
    ds = ds.shuffle(seed=42)
    n_val = max(1, int(len(ds) * args.val_frac))
    val_ds = ds.select(range(n_val))
    train_ds = ds.select(range(n_val, len(ds)))
    print(f"[split] train={len(train_ds)} val={len(val_ds)}")

    def fmt(r):
        user_msg = r["instruction"] + ("\n\n" + r["context"] if r.get("context") else "")
        prompt = tokenizer.apply_chat_template(
            [{"role": "user", "content": user_msg}],
            tokenize=False, add_generation_prompt=True)
        return {"prompt": prompt, "completion": r["response"]}
    cols_to_drop = [c for c in train_ds.column_names if c not in ("prompt", "completion")]
    train_ds = train_ds.map(fmt, remove_columns=train_ds.column_names)
    val_ds = val_ds.map(fmt, remove_columns=val_ds.column_names)

    t0 = time.time()
    trainer = SFTTrainer(
        model=model, tokenizer=tokenizer,
        train_dataset=train_ds,
        args=SFTConfig(
            output_dir=str(out_dir / f"{args.user}-trainer"),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch_size,
            gradient_accumulation_steps=args.grad_accum,
            learning_rate=args.lr,
            warmup_steps=5,
            logging_steps=5,
            eval_strategy="no",
            save_strategy="no",
            max_seq_length=args.max_seq_length,
            optim="adamw_8bit",
            weight_decay=args.weight_decay,
            report_to="none",
            completion_only_loss=True,
        ),
    )
    train_out = trainer.train()
    timings["train_s"] = round(time.time() - t0, 2)
    train_loss = float(train_out.training_loss)
    print(f"[train] done in {timings['train_s']}s, final train_loss={train_loss:.4f}")

    # Save adapter NOW so we don't lose it if val/eval steps fail
    adapter_path = out_dir / f"{args.user}-r1.adapter"
    model.save_pretrained(str(adapter_path))
    print(f"[adapter] saved to {adapter_path}")

    # Manual val loss after training (one example at a time, no batching headaches)
    import torch
    model.eval()
    val_losses = []
    with torch.no_grad():
        for r in val_ds:
            full = r["prompt"] + r["completion"]
            try:
                ids = tokenizer(text=full, return_tensors="pt", truncation=True,
                                max_length=args.max_seq_length).to(model.device)
                prompt_len = tokenizer(text=r["prompt"], return_tensors="pt").input_ids.shape[1]
                labels = ids.input_ids.clone()
                labels[:, :prompt_len] = -100
                out = model(**ids, labels=labels)
                val_losses.append(float(out.loss))
            except Exception as e:
                print(f"[val] skip: {e}")
    val_loss = sum(val_losses) / len(val_losses) if val_losses else float("nan")
    print(f"[val] manual val loss over {len(val_losses)} examples: {val_loss:.4f}")
    model.train()

    # Tuned
    t0 = time.time()
    tuned_out = run_inference(model, tokenizer, eval_prompts, args.max_eval_tokens)
    timings["tuned_s"] = round(time.time() - t0, 2)

    # ROUGE-L + action-item F1 scoring
    import re
    def extract_action_items(text):
        items = []
        for line in text.split("\n"):
            line = line.strip().lstrip("-*•").strip()
            m = re.match(r"^([A-Z][\w .'-]+?):\s*(.+)$", line)
            if m:
                owner = m.group(1).strip().lower()
                task = re.sub(r"\W+", " ", m.group(2)).strip().lower()
                items.append((owner, task))
        return items

    def action_item_f1(pred, ref):
        p_items = extract_action_items(pred)
        r_items = extract_action_items(ref)
        if not p_items and not r_items: return 1.0
        if not p_items or not r_items: return 0.0
        # Match: same owner + token overlap on task >= 0.4
        def task_overlap(a, b):
            sa, sb = set(a.split()), set(b.split())
            if not sa or not sb: return 0
            return len(sa & sb) / max(len(sa), len(sb))
        matched_r = set()
        tp = 0
        for po, pt in p_items:
            for j, (ro, rt) in enumerate(r_items):
                if j in matched_r: continue
                if po == ro and task_overlap(pt, rt) >= 0.4:
                    tp += 1; matched_r.add(j); break
        prec = tp / len(p_items)
        rec = tp / len(r_items)
        return 2*prec*rec/(prec+rec) if (prec+rec) else 0.0

    scores = {}
    for p in eval_prompts:
        ref = p.get("reference", "")
        if ref:
            scores[p["id"]] = {
                "baseline_rouge_l": round(rouge_l(base_out[p["id"]], ref), 4),
                "tuned_rouge_l": round(rouge_l(tuned_out[p["id"]], ref), 4),
                "baseline_action_f1": round(action_item_f1(base_out[p["id"]], ref), 4),
                "tuned_action_f1": round(action_item_f1(tuned_out[p["id"]], ref), 4),
            }
    avg_base = sum(s["baseline_rouge_l"] for s in scores.values()) / len(scores) if scores else 0
    avg_tuned = sum(s["tuned_rouge_l"] for s in scores.values()) / len(scores) if scores else 0
    avg_base_f1 = sum(s["baseline_action_f1"] for s in scores.values()) / len(scores) if scores else 0
    avg_tuned_f1 = sum(s["tuned_action_f1"] for s in scores.values()) / len(scores) if scores else 0

    # Compare md
    lines = [f"# Compare v4\n\n",
             f"**Config:** {json.dumps(vars(args))}\n\n",
             f"**Train loss (final):** {train_loss:.4f}\n\n",
             f"**Timings (s):** {json.dumps(timings)}\n\n",
             f"**ROUGE-L avg:** baseline={avg_base:.4f} → tuned={avg_tuned:.4f}\n\n",
             f"**Action-item F1 avg:** baseline={avg_base_f1:.4f} → tuned={avg_tuned_f1:.4f}\n\n"]
    for p in eval_prompts:
        pid = p["id"]
        lines.append(f"## {pid}\n\n")
        if pid in scores:
            lines.append(f"_ROUGE-L: baseline={scores[pid]['baseline_rouge_l']} → tuned={scores[pid]['tuned_rouge_l']}_\n\n")
        lines.append(f"### Baseline\n\n{base_out[pid]}\n\n### Tuned\n\n{tuned_out[pid]}\n\n")
        if "reference" in p:
            lines.append(f"### Reference\n\n{p['reference']}\n\n")
    compare_path = out_dir / f"{args.user}-r1.compare.md"
    compare_path.write_text("".join(lines))

    # Verdict
    n_shifted = sum(1 for p in eval_prompts if base_out[p["id"]] != tuned_out[p["id"]])
    eval_history = [{"eval_loss": val_loss}]
    print(f"\n{'='*60}")
    print(f"VERDICT: {n_shifted}/{len(eval_prompts)} shifted")
    print(f"ROUGE-L avg:       baseline={avg_base:.4f} → tuned={avg_tuned:.4f}")
    print(f"Action-item F1:    baseline={avg_base_f1:.4f} → tuned={avg_tuned_f1:.4f}")
    print(f"Val loss: {val_loss:.4f}, Train loss final: {train_loss:.4f}")
    print(f"{'='*60}")

    log = {
        "user": args.user, "config": vars(args),
        "train_loss": train_loss, "timings_s": timings,
        "rouge_l_avg": {"baseline": avg_base, "tuned": avg_tuned},
        "action_f1_avg": {"baseline": avg_base_f1, "tuned": avg_tuned_f1},
        "per_prompt_scores": scores,
        "val_loss": val_loss,
        "verdict_shifted": f"{n_shifted}/{len(eval_prompts)}",
        "baseline_outputs": base_out,
        "tuned_outputs": tuned_out,
    }
    (out_dir / f"{args.user}-r1.json").write_text(json.dumps(log, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
