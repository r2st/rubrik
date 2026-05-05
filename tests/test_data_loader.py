"""Tests for the data loader."""
from __future__ import annotations

from src import data_loader


def test_load_all_meetings_returns_100(raw_meetings) -> None:
    assert len(raw_meetings) == 100


def test_meeting_has_required_fields(raw_meetings) -> None:
    m = raw_meetings[0]
    assert m.meeting_id
    assert m.info.get("title")
    assert m.info.get("startTime")
    assert m.transcript.get("data")
    assert m.summary.get("summary")


def test_meeting_full_text_concatenates_sentences(raw_meetings) -> None:
    m = raw_meetings[0]
    assert len(m.full_text) > 0
    # full_text should contain content from at least the first sentence
    first_sentence = m.sentences[0]["sentence"]
    assert first_sentence[:30] in m.full_text


def test_meetings_dataframe_columns(raw_meetings) -> None:
    df = data_loader.meetings_to_dataframe(raw_meetings)
    expected = {"meeting_id", "title", "start_time", "duration_min",
                "sentiment_score", "full_transcript", "action_items", "topics"}
    assert expected.issubset(set(df.columns))
    assert len(df) == 100


def test_sentences_dataframe_has_per_sentence_sentiment(raw_meetings) -> None:
    s = data_loader.sentences_dataframe(raw_meetings)
    assert {"meeting_id", "speaker", "sentence", "sentiment", "time"}.issubset(s.columns)
    assert s["sentiment"].isin(["positive", "neutral", "negative"]).all()


def test_speakers_dataframe_durations_are_positive(raw_meetings) -> None:
    sp = data_loader.speakers_dataframe(raw_meetings)
    assert (sp["duration"] > 0).all()
    assert (sp["end_ts"] >= sp["start_ts"]).all()
