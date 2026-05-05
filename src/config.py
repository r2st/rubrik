"""Configuration: keyword maps, thresholds, and stop words.

Centralizing these makes the categorization rules auditable and tunable
without touching analysis logic.
"""
from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = PROJECT_ROOT.parent / "interview-assignment" / "dataset"
OUTPUT_DIR = PROJECT_ROOT / "output"

# ---------------------------------------------------------------------------
# Call type rules — order matters (first match wins)
# ---------------------------------------------------------------------------
CALL_TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("support", re.compile(r"^support case", re.I)),
    ("external", re.compile(r"\baegis\s*/|^urgent:|^escalation:", re.I)),
    # internal is the fallback
]

# ---------------------------------------------------------------------------
# Product area keywords (multi-label — a meeting can touch several products)
# ---------------------------------------------------------------------------
PRODUCT_KEYWORDS: dict[str, list[str]] = {
    "Detect": ["detect", "threat", "monitoring", "siem", "alert", "false positive"],
    "Comply": ["comply", "compliance", "soc 2", "soc2", "iso 27001", "hipaa", "pci",
               "audit", "control", "evidence"],
    "Protect": ["protect", "backup", "restore", "recovery", "snapshot", "logvault"],
    "Identity": ["identity", "sso", "saml", "ldap", "scim", "mfa", "okta", "provisioning"],
}

# ---------------------------------------------------------------------------
# Meeting purpose rules — first match wins, ordered by specificity
# ---------------------------------------------------------------------------
PURPOSE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("Incident Response",      re.compile(r"\boutage|incident|war room|^urgent:|^escalation:", re.I)),
    ("Support Resolution",     re.compile(r"^support case", re.I)),
    ("Renewal/Contract",       re.compile(r"\brenewal|contract", re.I)),
    ("Engineering Cadence",    re.compile(r"\bsprint|standup|retro\b", re.I)),
    ("Planning/Strategy",      re.compile(r"\broadmap|planning", re.I)),
    ("Competitive Intelligence", re.compile(r"\bcompetitive|win/loss|vendor comparison", re.I)),
    ("Customer Onboarding",    re.compile(r"\bonboarding|kickoff|deployment", re.I)),
    ("Product Feedback",       re.compile(r"\bfeedback|demo\b", re.I)),
    ("Company Update",         re.compile(r"\ball hands", re.I)),
    ("Review",                 re.compile(r"\breview\b", re.I)),
]
DEFAULT_PURPOSE = "Account Management"

# ---------------------------------------------------------------------------
# Customer name extraction (for external meetings)
# Matches "Aegis / <Customer> - ..." and "URGENT: <Customer> - ..."
# ---------------------------------------------------------------------------
CUSTOMER_REGEXES: list[re.Pattern[str]] = [
    re.compile(r"aegis\s*/\s*([^-—]+?)\s*[-—]", re.I),
    re.compile(r"^(?:urgent|escalation)\s*:\s*([^-—]+?)\s*[-—]", re.I),
]

# ---------------------------------------------------------------------------
# Clustering — filler words that pollute conversational TF-IDF
# ---------------------------------------------------------------------------
CONVERSATIONAL_FILLERS: list[str] = [
    "yeah", "okay", "ok", "like", "just", "think", "know", "right", "going",
    "want", "let", "got", "one", "thing", "things", "need", "good", "sure",
    "really", "actually", "mean", "said", "say", "way", "look", "come", "make",
    "time", "kind", "people", "work", "get", "ll", "ve", "uh", "um", "ah",
    "stuff", "bit", "yes", "no", "guys", "guess", "probably", "maybe",
]

# ---------------------------------------------------------------------------
# Risk scoring weights (for customer churn signal)
# Each component is normalized to roughly 0..1 then weighted.
# ---------------------------------------------------------------------------
RISK_WEIGHTS = {
    "low_sentiment": 0.5,    # how far below neutral
    "churn_signals": 0.3,    # explicit churn moments
    "negative_pivots": 0.2,  # within-meeting sentiment crashes
}

CHURN_RISK_THRESHOLDS = {
    "high": 0.40,
    "medium": 0.25,
}

# ---------------------------------------------------------------------------
# Competitive language detection
# ---------------------------------------------------------------------------
COMPETITIVE_KEYWORDS: list[str] = [
    "competitor", "competitive", "alternative", "evaluate", "evaluating",
    "compared to", "versus", "better than", "cheaper than", "switch to",
    "looking at other", "other vendors",
]
