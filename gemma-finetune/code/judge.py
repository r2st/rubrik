"""Claude-as-judge: grade baseline + tuned outputs against reference."""
import json, sys, os
from pathlib import Path
from anthropic import Anthropic

RUN_JSON = sys.argv[1]  # e.g. attendee-12-v4/attendee-12-v4-r1.json
EVAL_JSON = sys.argv[2]  # rubrik_eval_prompts.json

run = json.loads(Path(RUN_JSON).read_text())
prompts = json.loads(Path(EVAL_JSON).read_text())["prompts"]
refs = {p["id"]: p["reference"] for p in prompts}

JUDGE_PROMPT = """You are grading a meeting-summary generation model. Score the candidate output 1-5 on each axis. Be strict — 5 means "indistinguishable from a senior human's notes". 4 means "good, minor issues". 3 means "usable but rough". 2 means "significant problems". 1 means "unusable / off-task".

REFERENCE SUMMARY (gold):
{reference}

CANDIDATE OUTPUT (to grade):
{candidate}

Respond with ONLY a JSON object, no preamble:
{{"faithfulness": <1-5>, "completeness": <1-5>, "format": <1-5>, "hallucinations": <1-5>, "notes": "<one sentence>"}}

faithfulness: do the facts match the reference?
completeness: are the key points (parties, decisions, actions) all present?
format: paragraph + "Action items:" with "Owner: task" bullets like the reference?
hallucinations: 5 = none, 1 = many invented facts."""

client = Anthropic()

def judge(reference, candidate):
    msg = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(reference=reference, candidate=candidate)}],
    )
    text = msg.content[0].text.strip()
    # Strip code fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)

results = {}
for pid in run["baseline_outputs"]:
    ref = refs[pid]
    print(f"[judge] {pid}...")
    base_score = judge(ref, run["baseline_outputs"][pid])
    tuned_score = judge(ref, run["tuned_outputs"][pid])
    results[pid] = {"baseline": base_score, "tuned": tuned_score}

# Aggregate
def avg(scores, key):
    return sum(s[key] for s in scores) / len(scores) if scores else 0
axes = ["faithfulness", "completeness", "format", "hallucinations"]
base_scores = [r["baseline"] for r in results.values()]
tuned_scores = [r["tuned"] for r in results.values()]
print("\n========== Claude-as-judge avg (1-5) ==========")
for ax in axes:
    b, t = avg(base_scores, ax), avg(tuned_scores, ax)
    print(f"  {ax:20s}  baseline={b:.2f}  tuned={t:.2f}  delta={t-b:+.2f}")

# Save
out = {"per_prompt": results,
       "avg_baseline": {ax: avg(base_scores, ax) for ax in axes},
       "avg_tuned": {ax: avg(tuned_scores, ax) for ax in axes}}
out_path = Path(RUN_JSON).with_name(Path(RUN_JSON).stem + ".judge.json")
out_path.write_text(json.dumps(out, indent=2))
print(f"\n[saved] {out_path}")
