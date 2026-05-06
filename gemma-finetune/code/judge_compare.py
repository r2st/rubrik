"""Run Claude-as-judge on v3 and v4 outputs, compare side by side.

Reads outputs from compare.md (v3) or run JSON (v4) — both end up as
{prompt_id: {baseline, tuned}} dicts.
"""
import json, re, sys, os
from pathlib import Path
from anthropic import Anthropic

EVAL_JSON = Path("/Users/dev/projects/interview/Nebius/rubrik_eval_prompts.json")
prompts = json.loads(EVAL_JSON.read_text())["prompts"]
refs = {p["id"]: p["reference"] for p in prompts}

def parse_compare_md(path):
    """Extract {pid: {baseline, tuned}} from a compare.md."""
    text = Path(path).read_text()
    out = {}
    # Split on "## meeting-N" headers
    sections = re.split(r"\n## (meeting-\d+)\n", text)
    for i in range(1, len(sections), 2):
        pid = sections[i]
        body = sections[i+1]
        b = re.search(r"### Baseline\n+(.+?)\n+### Tuned", body, re.DOTALL)
        t = re.search(r"### Tuned\n+(.+?)(?:\n+### Reference|\n+## |\Z)", body, re.DOTALL)
        if b and t:
            out[pid] = {"baseline": b.group(1).strip(), "tuned": t.group(1).strip()}
    return out

def load_outputs(run_dir):
    """Return {pid: {baseline, tuned}} from a run directory."""
    run_dir = Path(run_dir)
    json_path = next(run_dir.glob("*-r1.json"))
    md_path = next(run_dir.glob("*-r1.compare.md"))
    data = json.loads(json_path.read_text())
    if "baseline_outputs" in data:
        return {pid: {"baseline": data["baseline_outputs"][pid],
                      "tuned": data["tuned_outputs"][pid]} for pid in data["baseline_outputs"]}
    return parse_compare_md(md_path)

JUDGE_PROMPT = """You are grading a meeting-summary generation model. Score the candidate output 1-5 on each axis. Be strict — 5 means "indistinguishable from a senior human's notes". 4 = "good, minor issues". 3 = "usable but rough". 2 = "significant problems". 1 = "unusable / off-task".

REFERENCE SUMMARY (gold):
{reference}

CANDIDATE OUTPUT (to grade):
{candidate}

Respond with ONLY a JSON object, no preamble, no markdown:
{{"faithfulness": <1-5>, "completeness": <1-5>, "format": <1-5>, "hallucinations": <1-5>, "notes": "<one sentence>"}}

faithfulness: do the facts match the reference?
completeness: are key points (parties, decisions, actions) present?
format: paragraph + "Action items:" with "Owner: task" bullets like reference?
hallucinations: 5 = none, 1 = many invented facts."""

client = Anthropic()

def judge(reference, candidate):
    msg = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(reference=reference, candidate=candidate)}],
    )
    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)

axes = ["faithfulness", "completeness", "format", "hallucinations"]

def avg(scores, key):
    return sum(s[key] for s in scores) / len(scores) if scores else 0

results = {}
runs = {
    "v3": "/Users/dev/projects/interview/Nebius/attendee-12-v3",
    "v4": "/Users/dev/projects/interview/Nebius/attendee-12-v4",
}
baseline_scored = False
baseline_scores = []

for run_name, run_dir in runs.items():
    print(f"\n========== {run_name} ==========")
    outputs = load_outputs(run_dir)
    tuned_scores = []
    for pid, pair in outputs.items():
        ref = refs[pid]
        if not baseline_scored:
            print(f"[judge baseline] {pid}...")
            baseline_scores.append(judge(ref, pair["baseline"]))
        print(f"[judge {run_name} tuned] {pid}...")
        tuned_scores.append(judge(ref, pair["tuned"]))
    baseline_scored = True
    results[run_name] = tuned_scores

print("\n========== Claude-as-judge averages (1-5) ==========")
print(f"{'axis':20s}  {'baseline':>10s}  {'v3 tuned':>10s}  {'v4 tuned':>10s}")
for ax in axes:
    print(f"{ax:20s}  {avg(baseline_scores, ax):>10.2f}  {avg(results['v3'], ax):>10.2f}  {avg(results['v4'], ax):>10.2f}")

out = {
    "axes": axes,
    "baseline": {ax: avg(baseline_scores, ax) for ax in axes},
    "v3_tuned": {ax: avg(results["v3"], ax) for ax in axes},
    "v4_tuned": {ax: avg(results["v4"], ax) for ax in axes},
    "raw_baseline": baseline_scores,
    "raw_v3": results["v3"],
    "raw_v4": results["v4"],
}
Path("/Users/dev/projects/interview/Nebius/judge_results.json").write_text(json.dumps(out, indent=2))
print("\n[saved] judge_results.json")
