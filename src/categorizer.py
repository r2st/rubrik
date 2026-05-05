"""Rule-based categorization of meetings into call type, purpose, product area.

All rules are config-driven (see `src/config.py`) so they can be tuned
without touching this file.
"""
from __future__ import annotations

import pandas as pd

from . import config


def classify_call_type(title: str) -> str:
    """Classify into 'support' | 'external' | 'internal'."""
    for label, pattern in config.CALL_TYPE_PATTERNS:
        if pattern.search(title or ""):
            return label
    return "internal"


def classify_purpose(title: str) -> str:
    """First-match purpose classifier (rules ordered by specificity)."""
    for label, pattern in config.PURPOSE_RULES:
        if pattern.search(title or ""):
            return label
    return config.DEFAULT_PURPOSE


def detect_product_areas(text: str) -> list[str]:
    """Multi-label product detection — one meeting can touch several products."""
    text_lc = (text or "").lower()
    found: list[str] = []
    for product, keywords in config.PRODUCT_KEYWORDS.items():
        if any(kw in text_lc for kw in keywords):
            found.append(product)
    return found or ["General"]


def extract_customer(title: str) -> str | None:
    """Extract customer name from external meeting titles. Returns None for internal."""
    for regex in config.CUSTOMER_REGEXES:
        match = regex.search(title or "")
        if match:
            return match.group(1).strip()
    return None


def annotate(df: pd.DataFrame) -> pd.DataFrame:
    """Add categorization columns to a meetings DataFrame.

    Columns added: call_type, meeting_purpose, product_areas, primary_product, customer
    """
    out = df.copy()
    out["call_type"] = out["title"].apply(classify_call_type)
    out["meeting_purpose"] = out["title"].apply(classify_purpose)
    combined = out["title"].fillna("") + " " + out["summary_text"].fillna("")
    out["product_areas"] = combined.apply(detect_product_areas)
    out["primary_product"] = out["product_areas"].apply(lambda xs: xs[0])
    out["customer"] = out["title"].apply(extract_customer)
    return out
