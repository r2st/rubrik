"""Semantic validation of the analysis pipeline.

Unit tests verify the rules behave as written. This script asks a different
question: do those rules actually hold up against the dataset? It runs a series
of audits and prints a one-page health report with PASS / WARN / FAIL flags.

Run:  python validate.py
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from src import categorizer, clustering, data_loader, sentiment


# ---------------------------------------------------------------------------
# Check primitives
# ---------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    status: str   # PASS | WARN | FAIL
    detail: str


def _flag(value: float, warn_above: float, fail_above: float) -> str:
    if value >= fail_above:
        return "FAIL"
    if value >= warn_above:
        return "WARN"
    return "PASS"


# ---------------------------------------------------------------------------
# Individual audits
# ---------------------------------------------------------------------------
def check_rule_coverage(df: pd.DataFrame) -> Check:
    """Catch-all bucket size — too many meetings landing in 'Account Management'
    means our purpose rules are leaving signal on the table."""
    catchall = (df["meeting_purpose"] == "Account Management").sum()
    pct = catchall / len(df) * 100
    return Check(
        name="Rule coverage (purpose)",
        status=_flag(pct, warn_above=20, fail_above=35),
        detail=f"{catchall}/{len(df)} ({pct:.0f}%) meetings landed in catch-all 'Account Management'",
    )


def check_customer_extraction(df: pd.DataFrame) -> Check:
    """Every external meeting should yield a customer name."""
    external = df[df["call_type"] == "external"]
    missing = external["customer"].isna().sum()
    pct = missing / max(len(external), 1) * 100
    return Check(
        name="Customer extraction (external)",
        status=_flag(pct, warn_above=5, fail_above=15),
        detail=f"{missing}/{len(external)} external meetings have no customer ({pct:.0f}%)",
    )


def check_product_cross_reference(df: pd.DataFrame) -> Check:
    """Cross-reference rule-based product detection against the dataset's own
    `topics` field. They should overlap on >70% of meetings that have product
    keywords in their summary topics.
    """
    product_topic_keywords = {
        "Detect": ["detect", "threat", "monitoring"],
        "Comply": ["compliance", "comply", "audit"],
        "Protect": ["backup", "protect", "recovery"],
        "Identity": ["identity", "sso", "authentication"],
    }

    agree = 0
    total = 0
    for _, row in df.iterrows():
        topic_text = " ".join(row["topics"]).lower()
        topic_products: set[str] = set()
        for prod, keywords in product_topic_keywords.items():
            if any(kw in topic_text for kw in keywords):
                topic_products.add(prod)
        if not topic_products:
            continue
        total += 1
        rule_products = set(row["product_areas"])
        if topic_products & rule_products:
            agree += 1

    pct_agreement = agree / max(total, 1) * 100
    return Check(
        name="Product cross-reference (rules ↔ topics field)",
        status="PASS" if pct_agreement >= 70 else "WARN" if pct_agreement >= 50 else "FAIL",
        detail=f"{agree}/{total} ({pct_agreement:.0f}%) meetings where rule-based product "
               f"matches the dataset's own topic tags",
    )


def check_data_quality(df: pd.DataFrame, sentences_df: pd.DataFrame,
                       speakers_df: pd.DataFrame) -> list[Check]:
    """Look for missing or pathological data."""
    out: list[Check] = []

    empty_transcripts = (df["num_sentences"] == 0).sum()
    out.append(Check(
        "Data quality: transcripts",
        "PASS" if empty_transcripts == 0 else "FAIL",
        f"{empty_transcripts} meetings with empty transcripts",
    ))

    no_speakers = df[~df["meeting_id"].isin(speakers_df["meeting_id"].unique())]
    out.append(Check(
        "Data quality: speakers",
        "PASS" if len(no_speakers) == 0 else "WARN",
        f"{len(no_speakers)} meetings with no speaker segments",
    ))

    weird_duration = ((df["duration_min"] <= 0) | (df["duration_min"] > 120)).sum()
    out.append(Check(
        "Data quality: durations",
        "PASS" if weird_duration == 0 else "WARN",
        f"{weird_duration} meetings with duration <=0 or >120 min",
    ))

    no_sentiment = df["sentiment_score"].isna().sum()
    out.append(Check(
        "Data quality: sentiment scores",
        "PASS" if no_sentiment == 0 else "FAIL",
        f"{no_sentiment} meetings missing sentiment_score",
    ))

    return out


def check_cluster_homogeneity(df: pd.DataFrame) -> Check:
    """A cluster dominated by one meeting purpose suggests it's just re-discovering
    a structural category we already get for free from rules. Mild concern, not a fail."""
    if "content_cluster" not in df.columns:
        return Check("Cluster homogeneity", "WARN", "no clusters present — run pipeline first")

    redundant = 0
    for cid, group in df.groupby("content_cluster"):
        purpose_share = group["meeting_purpose"].value_counts(normalize=True).iloc[0]
        if purpose_share > 0.85:
            redundant += 1

    return Check(
        name="Cluster homogeneity",
        status="PASS" if redundant <= 1 else "WARN",
        detail=f"{redundant} cluster(s) >85% dominated by a single meeting purpose "
               f"(may be redundant with rule-based categorization)",
    )


def check_sentiment_alignment(df: pd.DataFrame) -> Check:
    """Per-sentence sentiment trajectory should roughly agree with meeting-level
    score. Wildly disagreeing meetings indicate a labeling inconsistency."""
    aligned = df.dropna(subset=["share_negative"]).copy()
    aligned["meeting_below_neutral"] = aligned["sentiment_score"] < 3.0
    aligned["sentence_mostly_negative"] = aligned["share_negative"] > 0.4
    disagree = (aligned["meeting_below_neutral"] != aligned["sentence_mostly_negative"]).sum()
    pct = disagree / max(len(aligned), 1) * 100
    return Check(
        name="Sentiment alignment (meeting ↔ sentence)",
        status=_flag(pct, warn_above=25, fail_above=40),
        detail=f"{disagree}/{len(aligned)} ({pct:.0f}%) meetings where the meeting-level "
               f"and sentence-level sentiment disagree on direction",
    )


def check_churn_risk_distribution(df: pd.DataFrame, health: pd.DataFrame) -> Check:
    """Risk tiers should be a useful triage signal — neither everyone-is-fine nor everyone-is-on-fire."""
    if health.empty:
        return Check("Churn risk distribution", "WARN", "no customer data")
    high = (health["risk_tier"] == "🔴 high").sum()
    pct_high = high / len(health) * 100
    if 5 <= pct_high <= 30:
        status = "PASS"
    elif pct_high < 5 or pct_high > 50:
        status = "WARN"
    else:
        status = "PASS"
    return Check(
        name="Churn risk distribution",
        status=status,
        detail=f"{high}/{len(health)} ({pct_high:.0f}%) customers in 🔴 high tier",
    )


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------
_STATUS_GLYPH = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}


def _render(checks: list[Check]) -> int:
    """Print the report and return the number of FAILs."""
    print("\n" + "═" * 78)
    print(" VALIDATION REPORT".ljust(78))
    print("═" * 78)
    fails = 0
    for c in checks:
        glyph = _STATUS_GLYPH[c.status]
        print(f"\n {glyph} {c.name:<48} [{c.status}]")
        print(f"      {c.detail}")
        if c.status == "FAIL":
            fails += 1
    print("\n" + "═" * 78)
    counts = Counter(c.status for c in checks)
    print(f" Summary: {counts.get('PASS', 0)} pass · {counts.get('WARN', 0)} warn · "
          f"{counts.get('FAIL', 0)} fail   ({len(checks)} checks)")
    print("═" * 78 + "\n")
    return fails


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    print("Loading data...")
    raw = data_loader.load_all_meetings()
    df = data_loader.meetings_to_dataframe(raw)
    sentences_df = data_loader.sentences_dataframe(raw)
    speakers_df = data_loader.speakers_dataframe(raw)

    print("Running pipeline (categorize + trajectories + clusters)...")
    df = categorizer.annotate(df)
    df["num_action_items"] = df["action_items"].apply(len)
    df = sentiment.add_trajectories(df, sentences_df)
    cluster_result = clustering.cluster_transcripts(df["full_transcript"])
    df["content_cluster"] = cluster_result.labels

    from src import insights
    health = insights.customer_health(df)

    checks: list[Check] = []
    checks.append(check_rule_coverage(df))
    checks.append(check_customer_extraction(df))
    checks.append(check_product_cross_reference(df))
    checks.extend(check_data_quality(df, sentences_df, speakers_df))
    checks.append(check_cluster_homogeneity(df))
    checks.append(check_sentiment_alignment(df))
    checks.append(check_churn_risk_distribution(df, health))

    return _render(checks)


if __name__ == "__main__":
    raise SystemExit(main())
