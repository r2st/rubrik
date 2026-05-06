"""Quick smoke test: load v3 adapter, summarize one held-out meeting."""
import json, sys
from pathlib import Path

ADAPTER = "/home/ubuntu/gemma-finetune/runs/attendee-12-v3/attendee-12-v3-r1.adapter"
EVAL = "/home/ubuntu/gemma-finetune/data/rubrik_eval_prompts.json"

print("[load] base + adapter via FastModel...")
from unsloth import FastModel
model, tokenizer = FastModel.from_pretrained(
    ADAPTER, max_seq_length=8192,
    load_in_4bit=True, full_finetuning=False,
)

prompts = json.loads(Path(EVAL).read_text())["prompts"]
p = prompts[0]
print(f"[infer] prompt id: {p['id']}, prompt chars: {len(p['prompt'])}")

ids = tokenizer.apply_chat_template(
    [{"role": "user", "content": [{"type": "text", "text": p["prompt"]}]}],
    return_tensors="pt", add_generation_prompt=True, tokenize=True,
).to(model.device)
gen = model.generate(ids, max_new_tokens=400, do_sample=False)
out = tokenizer.decode(gen[0][ids.shape[1]:], skip_special_tokens=True).strip()

print("\n========== TUNED OUTPUT ==========\n" + out)
print("\n========== REFERENCE ==========\n" + p["reference"])
