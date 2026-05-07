"""Generate synthetic edge-case meeting fixtures.

Produces the same JSON shape as the real dataset (`meeting-info.json`,
`transcript.json`, `summary.json`, optionally `speakers.json` /
`speaker-meta.json`). Idempotent — re-running overwrites the fixtures.

Each fixture targets a specific edge case enumerated in `docs/edge-cases.md`.
The hand-tailored cases live in `make_default_set()`; add more by writing a
new builder function and appending to `DEFAULT_CASES`.

Run:
    python tests/fixtures/synthetic/gen_synthetic.py            # produces all
    python tests/fixtures/synthetic/gen_synthetic.py --case multi_incident
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Helpers — write the JSON files in a fixture directory
# ---------------------------------------------------------------------------
def _write(target: Path, name: str, data: Any) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{name}.json").write_text(json.dumps(data, indent=2))


def _meeting_id(slug: str) -> str:
    """Use the slug directly so fixtures are easy to identify; real IDs are
    ULIDs but the loader doesn't care about the format."""
    return slug


def _make_speakers(turns: list[tuple[str, str]],
                   *, start_offset: float = 0.0) -> list[dict[str, Any]]:
    """Build speaker segments from a list of (speaker, text) turns.

    Each turn gets an estimated duration based on word count (≈3 words/sec).
    """
    out = []
    cursor = start_offset
    for speaker, text in turns:
        duration = max(2.0, len(text.split()) / 3.0)
        out.append({
            "speakerName": speaker,
            "timestamp": round(cursor, 2),
            "endTimeTs": round(cursor + duration, 2),
        })
        cursor += duration + 0.5
    return out


def _make_transcript(turns: list[tuple[str, str]],
                     sentiments: Optional[list[str]] = None,
                     ) -> dict[str, Any]:
    """Build a transcript.json from (speaker, text) turns.

    `sentiments` aligns 1:1 with turns; defaults to all 'neutral'.
    """
    if sentiments is None:
        sentiments = ["neutral"] * len(turns)
    speaker_ids: dict[str, int] = {}
    sentences = []
    cursor = 0.0
    for i, ((speaker, text), sent) in enumerate(zip(turns, sentiments)):
        if speaker not in speaker_ids:
            speaker_ids[speaker] = len(speaker_ids)
        duration = max(2.0, len(text.split()) / 3.0)
        sentences.append({
            "sentence": text,
            "speaker_name": speaker,
            "sentimentType": sent,
            "speaker_id": speaker_ids[speaker],
            "time": round(cursor, 2),
            "endTime": round(cursor + duration, 2),
            "averageConfidence": 0.92,
            "index": i,
        })
        cursor += duration + 0.5
    return {"data": sentences}


# ---------------------------------------------------------------------------
# Fixture builders — one per edge case
# ---------------------------------------------------------------------------
@dataclass
class Fixture:
    slug: str           # directory name + meeting_id
    title: str
    organizer: str
    invitees: list[str]
    turns: list[tuple[str, str]]
    sentiments: list[str] = field(default_factory=list)
    summary: str = ""
    action_items: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    sentiment_score: float = 3.0
    overall_sentiment: str = "neutral"
    key_moments: list[dict[str, Any]] = field(default_factory=list)
    duration_min: float = 10.0
    start_time: str = "2026-04-15T10:00:00.000Z"
    end_time: str = "2026-04-15T10:10:00.000Z"


def write_fixture(out_dir: Path, fx: Fixture) -> None:
    target = out_dir / fx.slug
    log.info("→ %s", target.relative_to(HERE.parent.parent.parent))

    _write(target, "meeting-info", {
        "meetingId": _meeting_id(fx.slug),
        "title": fx.title,
        "organizerEmail": fx.organizer,
        "host": fx.organizer,
        "startTime": fx.start_time,
        "endTime": fx.end_time,
        "duration": fx.duration_min,
        "allEmails": fx.invitees,
        "invitees": fx.invitees,
    })

    # Empty sentiments list = "default to all neutral" (matches the function's
    # original `is None` semantic; dataclass default_factory gave us [] instead).
    sentiments = fx.sentiments or None
    _write(target, "transcript", _make_transcript(fx.turns, sentiments))
    _write(target, "speakers", _make_speakers(fx.turns))

    speakers_seen = []
    for spk, _ in fx.turns:
        if spk not in speakers_seen:
            speakers_seen.append(spk)
    _write(target, "speaker-meta",
           {str(i): name for i, name in enumerate(speakers_seen)})

    _write(target, "summary", {
        "summary": fx.summary,
        "actionItems": fx.action_items,
        "topics": fx.topics,
        "overallSentiment": fx.overall_sentiment,
        "sentimentScore": fx.sentiment_score,
        "keyMoments": fx.key_moments,
        "meetingId": _meeting_id(fx.slug),
    })

    _write(target, "events", [
        {"participantName": fx.invitees[0].split("@")[0],
         "timestamp": 1, "type": "Join", "time": 0.0},
        {"participantName": fx.invitees[0].split("@")[0],
         "timestamp": 2, "type": "Leave",
         "time": fx.duration_min * 60},
    ])


# ---------------------------------------------------------------------------
# The standard edge-case set
# ---------------------------------------------------------------------------
def case_title_variants(out: Path) -> None:
    """Five meetings with non-canonical title formats — verifies regex robustness."""
    base = out / "title_variants"
    cases = [
        ("urgent_lowercase",
         "urgent: Pinnacle Insurance - Detect Outage Update",
         "Lowercase URGENT prefix"),
        ("multi_word_escalation",
         "ESCALATION URGENT: Atlas Precision - Complete Service Outage",
         "Compound prefix (ESCALATION + URGENT)"),
        ("nested_aegis",
         "Aegis / EMEA / Crestline Wealth - Q1 Renewal",
         "Multi-segment Aegis path (region + customer)"),
        ("aegis_no_separator",
         "Aegis Frostbyte AI Identity Module Q2 Planning",
         "No separator after Aegis (degenerate)"),
        ("trailing_punctuation",
         "Aegis / Helix Data — Post-Incident Review!",
         "Em-dash separator + exclamation"),
    ]
    for slug, title, summary in cases:
        write_fixture(base, Fixture(
            slug=slug,
            title=title,
            organizer="rep@aegiscloud.com",
            invitees=["rep@aegiscloud.com", "customer@example.com"],
            turns=[
                ("Rep", "Thanks for joining today, let's walk through the agenda."),
                ("Customer", "Sounds good, I have about thirty minutes."),
                ("Rep", "We'll cover the renewal terms and the service updates."),
            ],
            summary=summary,
            sentiment_score=3.4,
            topics=["renewal", "operations"],
        ))


def case_customer_unicode(out: Path) -> None:
    """Customer names with non-ASCII characters."""
    base = out / "customer_unicode"
    cases = [
        ("apostrophe", "Aegis / L'Oreal - Account Review", "L'Oreal"),
        ("umlaut",     "Aegis / Müller GmbH - Q3 Planning", "Müller GmbH"),
        ("hyphenated", "Aegis / Foo-Bar Industries - Renewal Discussion", "Foo-Bar Industries"),
    ]
    for slug, title, customer in cases:
        write_fixture(base, Fixture(
            slug=slug,
            title=title,
            organizer="rep@aegiscloud.com",
            invitees=["rep@aegiscloud.com", f"contact@{slug}.example.com"],
            turns=[
                ("Rep", f"Welcome, glad to have folks from {customer} on the call."),
                ("Customer", "Thanks. Let's get into the renewal numbers."),
                ("Rep", "Of course. Pulling up the contract now."),
            ],
            summary=f"Renewal discussion with {customer}.",
            sentiment_score=3.6,
            topics=["renewal"],
        ))


def case_net_new_product(out: Path) -> None:
    """Mention of a product not in the current keyword list — DetectPlus."""
    base = out / "net_new_product"
    write_fixture(base, Fixture(
        slug="detect_plus_preview",
        title="Aegis / Nimbus Platform - DetectPlus Early Access Preview",
        organizer="rep@aegiscloud.com",
        invitees=["rep@aegiscloud.com", "platform@nimbus.example.com"],
        turns=[
            ("Rep", "DetectPlus is the next iteration of our threat-detection product."),
            ("Customer", "How does it differ from regular Detect?"),
            ("Rep", "DetectPlus adds behavioral baselining and continuous tuning."),
        ],
        summary="Preview of DetectPlus, the next-generation threat-detection product.",
        sentiment_score=4.0,
        topics=["product preview", "DetectPlus"],
    ))
    write_fixture(base, Fixture(
        slug="comply_vault_intro",
        title="Aegis / Steelpoint Manufacturing - ComplyVault Walk-Through",
        organizer="rep@aegiscloud.com",
        invitees=["rep@aegiscloud.com", "soc@steelpoint.example.com"],
        turns=[
            ("Rep", "ComplyVault stores audit evidence with cryptographic attestation."),
            ("Customer", "Is this part of Comply v2 or a separate product?"),
            ("Rep", "It's a separate product, ComplyVault, available standalone."),
        ],
        summary="Introduction to ComplyVault, a standalone audit-evidence product.",
        sentiment_score=3.8,
        topics=["product introduction", "ComplyVault"],
    ))


def case_all_neutral(out: Path) -> None:
    """A long, deliberately-flat meeting — verifies trajectory math doesn't fire false friction signals."""
    base = out / "all_neutral"
    turns = [
        ("Lead",  "Let's go around the room with status updates."),
        ("Eng A", "Worked on the queue migration last week, no blockers."),
        ("Eng B", "Pushed two PRs, both reviewed and merged."),
        ("PM",    "Roadmap planning continues, no changes to the priority list."),
        ("Eng C", "Ramped on the new service, expect first PR by end of week."),
        ("Eng D", "Pair-programmed with Eng A on the queue work."),
        ("Lead",  "Anything else worth flagging?"),
        ("Eng A", "Nothing from me."),
        ("Eng B", "All quiet."),
        ("Lead",  "Great, let's wrap up."),
    ] * 20  # 200 sentences total
    write_fixture(base, Fixture(
        slug="weekly_status",
        title="Weekly Engineering Standup",
        organizer="lead@aegiscloud.com",
        invitees=["lead@aegiscloud.com",
                  "enga@aegiscloud.com", "engb@aegiscloud.com",
                  "engc@aegiscloud.com", "engd@aegiscloud.com",
                  "pm@aegiscloud.com"],
        turns=turns,
        sentiments=["neutral"] * len(turns),
        summary="Routine weekly standup. No blockers, no changes to priorities.",
        sentiment_score=3.0,
        overall_sentiment="neutral",
        topics=["status update", "planning"],
        duration_min=45.0,
    ))


def case_single_sentence(out: Path) -> None:
    """Degenerate input — verifies bucket math handles N < n_buckets."""
    base = out / "single_sentence"
    write_fixture(base, Fixture(
        slug="quick_sync",
        title="Aegis / Pineridge Systems - Quick Sync",
        organizer="rep@aegiscloud.com",
        invitees=["rep@aegiscloud.com", "ops@pineridge.example.com"],
        turns=[("Rep", "Just confirming the deployment is on track for tomorrow.")],
        sentiments=["neutral"],
        summary="One-line confirmation of tomorrow's deployment.",
        sentiment_score=3.5,
        topics=["deployment confirmation"],
        duration_min=1.0,
    ))


def case_multi_incident(out: Path) -> None:
    """Two incidents referenced in one meeting."""
    base = out / "multi_incident"
    write_fixture(base, Fixture(
        slug="dual_outage_review",
        title="ESCALATION: Bridgeport Health - Detect AND Comply Outage",
        organizer="rep@aegiscloud.com",
        invitees=["rep@aegiscloud.com", "exec@bridgeport.example.com"],
        turns=[
            ("Customer", "We had two simultaneous outages on Tuesday."),
            ("Customer", "Detect was down for forty minutes and Comply was down for an hour."),
            ("Rep",      "Yes, we treat these as related — the cascading failure in the event pipeline took both down."),
            ("Customer", "What's the remediation plan for each?"),
            ("Rep",      "Detect remediation is the redundant active-active node rollout."),
            ("Rep",      "Comply remediation is independent — separate circuit-breaker work."),
        ],
        sentiments=["negative", "negative", "neutral", "negative", "neutral", "neutral"],
        summary="Two simultaneous outages discussed (Detect + Comply) with separate remediation plans.",
        sentiment_score=2.3,
        overall_sentiment="negative",
        topics=["incident response", "remediation"],
        key_moments=[
            {"time": 5.0, "text": "Customer confirms two outages on Tuesday",
             "type": "concern", "speaker": "Customer"},
            {"time": 30.0, "text": "Detect remediation: redundant nodes",
             "type": "positive_pivot", "speaker": "Rep"},
        ],
        duration_min=20.0,
    ))


def case_historical_incident(out: Path) -> None:
    """An old outage discussed retrospectively, not currently impacting."""
    base = out / "historical_incident"
    write_fixture(base, Fixture(
        slug="postmortem_review",
        title="Aegis / Vanta Health Systems - Q2 Business Review",
        organizer="rep@aegiscloud.com",
        invitees=["rep@aegiscloud.com", "cio@vanta.example.com"],
        turns=[
            ("Rep",      "Looking back at Q1, the March outage was the only major event."),
            ("Customer", "We've been stable since then, six clean months."),
            ("Rep",      "Right — and the redundant infrastructure work prevented the May aftershock."),
            ("Customer", "Service has been excellent through Q2."),
        ],
        sentiments=["neutral", "positive", "positive", "positive"],
        summary="Q2 business review. References the March outage retrospectively as historical context.",
        sentiment_score=4.1,
        overall_sentiment="positive",
        topics=["business review", "reliability"],
        duration_min=30.0,
    ))


def case_internal_mentions_customer(out: Path) -> None:
    """Internal meeting that references a customer by name — classification ambiguity check."""
    base = out / "internal_mentions_customer"
    write_fixture(base, Fixture(
        slug="account_strategy_internal",
        title="Account Strategy Internal - Northstar Pharma",
        organizer="lead@aegiscloud.com",
        invitees=["lead@aegiscloud.com",
                  "rep@aegiscloud.com",
                  "vp@aegiscloud.com"],
        turns=[
            ("Lead", "Northstar Pharma is in the high-risk tier; let's plan the next quarter together."),
            ("Rep",  "I have the next renewal in October. They're talking about evaluating SentinelShield."),
            ("VP",   "Block on my calendar before the renewal call. We need a unified position internally."),
        ],
        sentiments=["neutral", "negative", "neutral"],
        summary="Internal-only strategy session about how to approach the Northstar Pharma renewal.",
        sentiment_score=3.0,
        overall_sentiment="mixed",
        topics=["account strategy", "renewal planning"],
        duration_min=30.0,
    ))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
DEFAULT_CASES: dict[str, Callable[[Path], None]] = {
    "title_variants": case_title_variants,
    "customer_unicode": case_customer_unicode,
    "net_new_product": case_net_new_product,
    "all_neutral": case_all_neutral,
    "single_sentence": case_single_sentence,
    "multi_incident": case_multi_incident,
    "historical_incident": case_historical_incident,
    "internal_mentions_customer": case_internal_mentions_customer,
}


def make_default_set(out_dir: Path) -> None:
    """Generate every default edge-case fixture."""
    log.info("Writing %d edge-case fixtures → %s/", len(DEFAULT_CASES), out_dir.relative_to(HERE.parent.parent.parent))
    for name, builder in DEFAULT_CASES.items():
        builder(out_dir)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=str(HERE), help="output directory")
    p.add_argument("--case", default=None,
                   choices=list(DEFAULT_CASES.keys()),
                   help="generate a single case (default: all)")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args()
    out = Path(args.out)
    if args.case:
        DEFAULT_CASES[args.case](out)
    else:
        make_default_set(out)


if __name__ == "__main__":
    main()
