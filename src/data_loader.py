"""Load raw meeting JSON files and shape them into analysis-ready DataFrames."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from . import config

_FILE_KEYS = ["meeting-info", "transcript", "speakers", "speaker-meta", "summary", "events"]


@dataclass
class Meeting:
    """In-memory representation of a single meeting and its raw artifacts."""
    meeting_id: str
    info: dict[str, Any] = field(default_factory=dict)
    transcript: dict[str, Any] = field(default_factory=dict)
    speakers: list[dict[str, Any]] = field(default_factory=list)
    speaker_meta: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def sentences(self) -> list[dict[str, Any]]:
        return self.transcript.get("data", []) or []

    @property
    def title(self) -> str:
        return self.info.get("title", "")

    @property
    def full_text(self) -> str:
        return " ".join(s["sentence"] for s in self.sentences)


def _load_json(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


def load_meeting(meeting_dir: Path) -> Meeting:
    """Load a single meeting directory into a Meeting dataclass."""
    raw: dict[str, Any] = {}
    for key in _FILE_KEYS:
        fpath = meeting_dir / f"{key}.json"
        if fpath.exists():
            raw[key.replace("-", "_")] = _load_json(fpath)
    return Meeting(
        meeting_id=meeting_dir.name,
        info=raw.get("meeting_info", {}),
        transcript=raw.get("transcript", {}),
        speakers=raw.get("speakers", []),
        speaker_meta=raw.get("speaker_meta", {}),
        summary=raw.get("summary", {}),
        events=raw.get("events", []),
    )


def load_all_meetings(dataset_path: Optional[Path] = None) -> list[Meeting]:
    """Load every meeting subdirectory under `dataset_path`."""
    base = dataset_path or config.DATASET_PATH
    if not base.exists():
        raise FileNotFoundError(f"Dataset not found at: {base}")
    return [load_meeting(p) for p in sorted(base.iterdir()) if p.is_dir()]


def meetings_to_dataframe(meetings: list[Meeting]) -> pd.DataFrame:
    """Project meetings into a flat DataFrame for analysis."""
    rows = []
    for m in meetings:
        rows.append({
            "meeting_id": m.meeting_id,
            "title": m.title,
            "organizer": m.info.get("organizerEmail", ""),
            "start_time": m.info.get("startTime"),
            "end_time": m.info.get("endTime"),
            "duration_min": m.info.get("duration", 0.0),
            "num_participants": len(m.info.get("allEmails", [])),
            "summary_text": m.summary.get("summary", ""),
            "action_items": m.summary.get("actionItems", []) or [],
            "topics": m.summary.get("topics", []) or [],
            "overall_sentiment": m.summary.get("overallSentiment", ""),
            "sentiment_score": m.summary.get("sentimentScore", 0.0),
            "key_moments": m.summary.get("keyMoments", []) or [],
            "num_sentences": len(m.sentences),
            "full_transcript": m.full_text,
        })
    df = pd.DataFrame(rows)
    df["start_time"] = pd.to_datetime(df["start_time"])
    df["end_time"] = pd.to_datetime(df["end_time"])
    return df


def speakers_dataframe(meetings: list[Meeting]) -> pd.DataFrame:
    """Per-speaker-segment table for talk-time analysis."""
    rows = []
    for m in meetings:
        for seg in m.speakers:
            rows.append({
                "meeting_id": m.meeting_id,
                "title": m.title,
                "speaker": seg["speakerName"],
                "start_ts": seg["timestamp"],
                "end_ts": seg["endTimeTs"],
                "duration": seg["endTimeTs"] - seg["timestamp"],
            })
    return pd.DataFrame(rows)


def sentences_dataframe(meetings: list[Meeting]) -> pd.DataFrame:
    """Per-sentence table — enables sentence-level sentiment trajectories."""
    rows = []
    for m in meetings:
        for s in m.sentences:
            rows.append({
                "meeting_id": m.meeting_id,
                "index": s.get("index"),
                "speaker": s.get("speaker_name"),
                "sentence": s.get("sentence", ""),
                "sentiment": s.get("sentimentType", "neutral"),
                "confidence": s.get("averageConfidence", 0.0),
                "time": s.get("time", 0.0),
            })
    return pd.DataFrame(rows)
