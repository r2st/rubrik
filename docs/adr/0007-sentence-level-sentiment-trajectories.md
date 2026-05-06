# ADR 0007: Use Sentence-Level Sentiment Trajectories

- **Status:** Accepted
- **Date:** 2026-04-22

## Context

The dataset provides **two sentiment signals per meeting**:
1. **Meeting-level score** (1–5) in `summary.json` — a single number summarizing the whole call
2. **Per-sentence labels** (positive / neutral / negative) in `transcript.json` — already pre-computed with average confidence 0.92

Most pipelines we'd build for this kind of data use only the meeting-level score and call it done. That misses the sentence-level signal entirely.

### What's hidden in the meeting-level score

Consider a meeting with sentiment score 3.4 — sounds neutral-positive, all is well. But the sentence-level trajectory might be `[0.5, 0.4, -0.6, 0.0, 0.2]`: a sharp friction moment in the middle followed by a recovery. The 3.4 average smoothed it over.

That friction moment is **coaching gold**: the exact 90 seconds of the meeting worth reviewing. Surfacing it requires the per-sentence layer.

## Decision

**Use both granularities.** The meeting-level score for headline metrics; the per-sentence labels for within-meeting trajectories.

For each meeting we compute:
- `trajectory` — bucket sentences into 5 equal segments, average the per-sentence score (`positive=+1, neutral=0, negative=-1`) per bucket
- `max_drop` — largest negative bucket-to-bucket delta (the friction signal)
- `share_negative` — fraction of sentences labeled negative

Implementation in `src/sentiment.py`. Surfaced through:
- The `negative_pivots` insight (meetings with `max_drop ≤ -0.5`)
- The customer churn risk score (within-meeting pivots are one of three weighted signals)
- The dashboard's per-meeting drill-down (the trajectory plot is the centerpiece)

## Consequences

**Positive**
- Surfaces 9 meetings with sharp within-call friction moments — a signal **invisible to anyone using only the meeting-level score**
- Differentiator vs. competing tools that consume the same dataset
- Free: the labels are already in the input

**Negative**
- Adds a small column set to the meetings DataFrame (`trajectory`, `max_drop`, `share_negative`)
- 22% of meetings show meeting-level vs sentence-level disagreement on direction (validation `check_sentiment_alignment` flags this) — interpretable, not a bug, but worth knowing

**Neutral**
- If the input shape ever changes (raw text only, no labels), we'd swap in a HuggingFace classifier — one-function change in `src/sentiment.py`. The downstream consumers don't care where the labels come from.

## When to revisit

- The input no longer provides per-sentence labels — switch to a classifier
- Sentence count per meeting drops below ~20 — 5-bucket trajectory becomes too coarse; consider 3 buckets or skip trajectory analysis on short meetings
- A new signal (per-speaker sentiment? per-topic sentiment?) becomes more valuable than within-meeting trajectories

## Related

- `src/sentiment.py` — `meeting_sentiment_trajectory`, `add_trajectories`
- `src/insights.py` — `negative_pivots`, `customer_health` (uses pivots as one risk signal)
- `docs/APPROACH.md` § 3 — Sentiment analysis verdict
- `tests/test_sentiment.py` — 8 unit tests for the trajectory math
