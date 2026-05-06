"""Build multi-task training data from Rubrik meeting dataset."""
import json, sys
from pathlib import Path

SRC = Path("/Users/dev/projects/interview/Rubrik/interview-assignment/dataset")
OUT_TRAIN = Path("/Users/dev/projects/interview/Nebius/rubrik_meetings.jsonl")
OUT_EVAL = Path("/Users/dev/projects/interview/Nebius/rubrik_eval_prompts.json")

INSTR_FULL = (
    "Summarize the following meeting transcript. Provide a concise summary "
    "paragraph followed by a bulleted list of action items "
    "(format each as 'Owner: task')."
)
INSTR_SUMMARY = "Write a concise one-paragraph summary of the following meeting transcript."
INSTR_ACTIONS = (
    "Extract all action items from the following meeting transcript. "
    "List each as a bullet point in the form 'Owner: task'. "
    "If there are none, write 'No action items'."
)
INSTR_ATTENDEES = (
    "List the attendees of the following meeting. "
    "Output one name per line, sorted alphabetically by first name."
)
INSTR_TITLE = (
    "Suggest a short, descriptive title (5-10 words) for the following meeting transcript. "
    "Output only the title, no quotation marks."
)

meetings = []
for mdir in sorted(SRC.iterdir()):
    if not mdir.is_dir():
        continue
    try:
        transcript = json.loads((mdir / "transcript.json").read_text())["data"]
        summary_obj = json.loads((mdir / "summary.json").read_text())
        info = json.loads((mdir / "meeting-info.json").read_text())
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        print(f"skip {mdir.name}: {e}", file=sys.stderr)
        continue

    transcript_text = "\n".join(
        f"{t.get('speaker_name','Unknown')}: {t['sentence']}" for t in transcript
    )
    summary = summary_obj.get("summary", "").strip()
    action_items = summary_obj.get("actionItems", []) or []
    actions_text = "\n".join(f"- {a}" for a in action_items) if action_items else "No action items"
    full_response = summary + (f"\n\nAction items:\n{actions_text}" if action_items else "")

    speakers = sorted({t.get("speaker_name", "Unknown") for t in transcript})
    attendees_text = "\n".join(speakers)

    meetings.append({
        "id": mdir.name,
        "context": transcript_text,
        "summary": summary,
        "actions": actions_text,
        "attendees": attendees_text,
        "title": info.get("title", "").strip(),
        "full": full_response,
    })

# Hold out last 5 meetings as eval
eval_meetings = meetings[-5:]
train_meetings = meetings[:-5]

# Multi-task expansion
train_rows = []
for m in train_meetings:
    train_rows.append({"instruction": INSTR_FULL, "context": m["context"], "response": m["full"]})
    train_rows.append({"instruction": INSTR_SUMMARY, "context": m["context"], "response": m["summary"]})
    train_rows.append({"instruction": INSTR_ACTIONS, "context": m["context"], "response": m["actions"]})
    train_rows.append({"instruction": INSTR_ATTENDEES, "context": m["context"], "response": m["attendees"]})
    if m["title"]:
        train_rows.append({"instruction": INSTR_TITLE, "context": m["context"], "response": m["title"]})

with OUT_TRAIN.open("w") as f:
    for r in train_rows:
        f.write(json.dumps(r) + "\n")

# Eval prompts: full task on each held-out meeting
eval_prompts = {
    "prompts": [
        {
            "id": f"meeting-{i+1}",
            "category": "summarization",
            "prompt": f"{INSTR_FULL}\n\n{m['context']}",
            "reference": m["full"],
        }
        for i, m in enumerate(eval_meetings)
    ]
}
OUT_EVAL.write_text(json.dumps(eval_prompts, indent=2))

print(f"train: {len(train_rows)} rows ({len(train_meetings)} meetings × 4 tasks) -> {OUT_TRAIN}")
print(f"eval:  {len(eval_meetings)} prompts -> {OUT_EVAL}")
print(f"train file size: {OUT_TRAIN.stat().st_size/1024:.1f} KB")
