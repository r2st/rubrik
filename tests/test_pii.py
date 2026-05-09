"""Tests for the PII redactor."""
from __future__ import annotations

from src.pii import PiiRedactor, default_redactor, redact


def test_redacts_email():
    out, summary = redact("Contact alice@example.com tomorrow.")
    assert out == "Contact <REDACTED:EMAIL> tomorrow."
    assert summary.counts == {"EMAIL": 1}
    assert summary.total == 1
    assert bool(summary)


def test_redacts_us_phone():
    out, _ = redact("Call (555) 123-4567 today.")
    assert "<REDACTED:PHONE>" in out
    assert "555" not in out


def test_redacts_e164_phone():
    out, _ = redact("Mobile +1 415-555-0199.")
    assert "<REDACTED:PHONE>" in out


def test_redacts_ssn():
    out, summary = redact("SSN 123-45-6789 on file.")
    assert "<REDACTED:SSN>" in out
    assert summary.counts.get("SSN") == 1


def test_redacts_credit_card_only_when_luhn_valid():
    """Random 16-digit number should NOT be redacted unless it Luhn-validates."""
    invalid = "1234 5678 9012 3456"
    out, summary = redact(f"Card {invalid}.")
    assert invalid in out
    assert "CREDIT_CARD" not in summary.counts

    # 4242 4242 4242 4242 is the Stripe-test Luhn-valid number.
    valid = "4242 4242 4242 4242"
    out, summary = redact(f"Card {valid}.")
    assert "<REDACTED:CREDIT_CARD>" in out
    assert summary.counts["CREDIT_CARD"] == 1


def test_redacts_ipv4():
    out, summary = redact("Origin 192.168.1.42 (internal)")
    assert "<REDACTED:IPV4>" in out
    assert summary.counts.get("IPV4") == 1


def test_redacts_api_keys():
    out, summary = redact("Authorization: sk-AbCdEfGhIjKlMnOpQrStUvWxYz1234567890")
    assert "<REDACTED:API_KEY>" in out
    assert summary.counts.get("API_KEY") == 1


def test_multiple_kinds_in_one_text():
    text = "Email me at jane@example.com or sk-1234567890abcdefghij1234567890."
    out, summary = redact(text)
    assert "<REDACTED:EMAIL>" in out
    assert "<REDACTED:API_KEY>" in out
    assert summary.counts == {"EMAIL": 1, "API_KEY": 1}
    assert summary.total == 2


def test_empty_text_returns_empty_summary():
    out, summary = redact("")
    assert out == ""
    assert not bool(summary)


def test_no_pii_passes_through_unchanged():
    text = "The customer mentioned the new pricing tier."
    out, summary = redact(text)
    assert out == text
    assert summary.total == 0


def test_has_pii_short_circuits_true():
    assert default_redactor.has_pii("Email alice@example.com") is True
    assert default_redactor.has_pii("Call (555) 123-4567") is True
    assert default_redactor.has_pii("clean text") is False


def test_redactor_is_deterministic():
    """Same input → identical output. Required for caching + idempotency."""
    text = "Reach me at me@example.com or +1 415-555-0100."
    a = default_redactor.redact(text)
    b = default_redactor.redact(text)
    assert a[0] == b[0]
    assert a[1].counts == b[1].counts


def test_custom_placeholder_format():
    r = PiiRedactor(placeholder_format="[[{kind}]]")
    out, _ = r.redact("Email alice@example.com please.")
    assert "[[EMAIL]]" in out
