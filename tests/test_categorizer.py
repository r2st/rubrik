"""Sanity tests for the rule-based categorizer.

These pin the rules to known examples from the dataset so future config
changes that break expected behavior fail loudly.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src import categorizer


@pytest.mark.parametrize("title,expected", [
    ("Support Case #9279 - Summit Trust Billing Inquiry", "support"),
    ("Aegis / Redwood Clinical - ISO 27001 Preparation", "external"),
    ("URGENT: Cobalt Software - Aegis Detect Dashboard Down", "external"),
    ("ESCALATION: Northstar Pharma - Detect Outage Impact", "external"),
    ("Weekly Engineering Standup", "internal"),
    ("All Hands - April Update", "internal"),
    ("Detect Outage - Remediation Plan Review", "internal"),
])
def test_classify_call_type(title: str, expected: str) -> None:
    assert categorizer.classify_call_type(title) == expected


@pytest.mark.parametrize("title,expected", [
    ("Detect Outage - Remediation Plan Review", "Incident Response"),
    ("URGENT: Cobalt Software - Aegis Detect Dashboard Down", "Incident Response"),
    ("Support Case #9279 - Billing", "Support Resolution"),
    ("Aegis / Atlas Precision - Annual Review & Renewal", "Renewal/Contract"),
    ("Detect Team - Sprint Planning", "Engineering Cadence"),
    ("Identity Team - Q2 Roadmap", "Planning/Strategy"),
    ("Win/Loss Analysis - Q1", "Competitive Intelligence"),
    ("Aegis / Coastal Living Co - Onboarding Kickoff", "Customer Onboarding"),
    ("Aegis / Frostbyte AI - Product Feedback", "Product Feedback"),
    ("All Hands - February Wrap", "Company Update"),
    ("SOC 2 Type II - Final Review", "Review"),
    ("Aegis / Crestline Wealth - Account Review", "Review"),
])
def test_classify_purpose(title: str, expected: str) -> None:
    assert categorizer.classify_purpose(title) == expected


@pytest.mark.parametrize("text,expected_subset", [
    ("Detect outage threat monitoring", {"Detect"}),
    ("SOC 2 audit compliance evidence", {"Comply"}),
    ("Backup recovery snapshot job", {"Protect"}),
    ("SAML SSO MFA Okta provisioning", {"Identity"}),
    ("Detect threat with Comply audit", {"Detect", "Comply"}),
])
def test_detect_product_areas(text: str, expected_subset: set[str]) -> None:
    detected = set(categorizer.detect_product_areas(text))
    assert expected_subset.issubset(detected), f"got {detected}"


def test_detect_product_areas_fallback() -> None:
    assert categorizer.detect_product_areas("Generic project meeting") == ["General"]


@pytest.mark.parametrize("title,expected", [
    ("Aegis / Redwood Clinical - ISO 27001 Preparation", "Redwood Clinical"),
    ("Aegis / Atlas Precision - Contract Discussion", "Atlas Precision"),
    ("URGENT: Blackridge Investments - Complete Loss of Threat Visibility",
     "Blackridge Investments"),
    ("ESCALATION: Northstar Pharma - Detect Outage Impact on Compliance",
     "Northstar Pharma"),
])
def test_extract_customer(title: str, expected: str) -> None:
    assert categorizer.extract_customer(title) == expected


def test_extract_customer_internal_returns_none() -> None:
    assert categorizer.extract_customer("Weekly Engineering Standup") is None
    assert categorizer.extract_customer("Support Case #9279 - Summit Trust") is None


def test_annotate_adds_columns() -> None:
    df = pd.DataFrame([
        {"title": "Aegis / Atlas Precision - Annual Review",
         "summary_text": "Detect product was discussed."},
        {"title": "Support Case #1 - Backup issue",
         "summary_text": "Customer needs help."},
    ])
    out = categorizer.annotate(df)
    assert {"call_type", "meeting_purpose", "product_areas",
            "primary_product", "customer"}.issubset(out.columns)
    assert out.loc[0, "call_type"] == "external"
    assert out.loc[0, "customer"] == "Atlas Precision"
    assert out.loc[1, "call_type"] == "support"
