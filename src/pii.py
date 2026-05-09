"""PII redaction — regex-based scrubber for outbound payloads.

Used in three places (per ADR 0012 §"Guardrails before any external call"):

  1. Frontier-LLM gateway — every prompt body is redacted before leaving
     the perimeter.
  2. ``OutboxEvent.payload`` — optional pre-publish redaction so events
     fanning out to ClickHouse / Iceberg / Search aren't carrying raw
     PII.
  3. Log scrubbing (opt-in via ``logging_config``) — when JSON logs
     include user-supplied content, this redactor cleans them.

Coverage
--------
Built-in regex redactors:
  - Email addresses
  - Phone numbers (US + E.164)
  - US Social Security Numbers
  - Credit-card-like 13–19-digit sequences (Luhn-validated)
  - IPv4 + IPv6
  - Common API-key shapes (``sk-…``, ``ghp_…``, ``AKIA…``)

Each match is replaced with a placeholder of the form
``<REDACTED:KIND>``. The redactor returns both the cleaned string and a
``RedactionSummary`` so callers can log what was redacted without
logging the redacted content itself.

For higher-recall PII (names, addresses, free-form personal data), the
regex layer is the floor — production paths should also pass output
through spaCy NER (``PiiRedactor.with_ner()``) when the optional
dependency is installed. We don't make ``spacy`` a hard requirement so
tests stay fast.

The redactor is **stateless and deterministic** — same input always
yields the same output, suitable for caching and idempotency-key
hashing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Built-in redactors — each (regex, label, optional validator)
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
)
# US 10-digit phone or E.164 international (loose; over-matches a bit)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
)
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
# Credit-card-like — Luhn-validated below to cut false positives
_CC_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_IPV4_RE = re.compile(
    r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?!\d)"
)
_IPV6_RE = re.compile(r"(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}(?![0-9a-fA-F:])")
_API_KEY_RES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "API_KEY"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "API_KEY"),
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "API_KEY"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "API_KEY"),
)


def _luhn_ok(digits: str) -> bool:
    """Luhn-checksum validation for credit-card numbers."""
    s = "".join(c for c in digits if c.isdigit())
    if not 13 <= len(s) <= 19:
        return False
    total = 0
    parity = len(s) % 2
    for i, d in enumerate(s):
        n = int(d)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
@dataclass
class RedactionSummary:
    """Counts of each PII kind that was redacted, no content."""
    counts: dict[str, int] = field(default_factory=dict)
    matched_chars: int = 0  # total length of matched substrings in the original

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def __bool__(self) -> bool:
        return self.total > 0

    def density(self, original_length: int) -> float:
        """Fraction of the original text that was inside a PII match.

        Resilient to placeholder length (the redacted output may be larger
        than the original because ``<REDACTED:EMAIL>`` is longer than
        ``a@b.co``). Density tells you "how much of the input was PII?"
        on a [0, 1] scale.
        """
        if original_length <= 0:
            return 0.0
        return min(1.0, self.matched_chars / original_length)


@dataclass
class PiiRedactor:
    """Stateless PII redactor over arbitrary text.

    Use ``redact(text)`` for a clean string + a summary of what was
    pulled. Use ``has_pii(text)`` for a cheap boolean precheck.
    """

    placeholder_format: str = "<REDACTED:{kind}>"

    def redact(self, text: str) -> tuple[str, RedactionSummary]:
        if not text:
            return text, RedactionSummary()
        summary = RedactionSummary()
        out = text

        def _apply(pattern: re.Pattern, kind: str, source: str) -> str:
            """Subn + count matched chars + bump kind counter."""
            matched_chars = sum(len(m.group(0)) for m in pattern.finditer(source))
            new, n = pattern.subn(self._sub(kind), source)
            if n:
                summary.counts[kind] = summary.counts.get(kind, 0) + n
                summary.matched_chars += matched_chars
            return new

        # API-key shapes first (most specific patterns).
        for pattern, kind in _API_KEY_RES:
            out = _apply(pattern, kind, out)

        out = _apply(_SSN_RE, "SSN", out)
        out = _apply(_EMAIL_RE, "EMAIL", out)

        # Credit cards — Luhn-validate to cut false positives.
        cc_match_chars = 0
        cc_count = 0
        def _cc_repl(m: re.Match) -> str:
            nonlocal cc_match_chars, cc_count
            if _luhn_ok(m.group(0)):
                cc_match_chars += len(m.group(0))
                cc_count += 1
                return self._sub("CREDIT_CARD")(m)
            return m.group(0)
        out = _CC_RE.sub(_cc_repl, out)
        if cc_count:
            summary.counts["CREDIT_CARD"] = cc_count
            summary.matched_chars += cc_match_chars

        out = _apply(_PHONE_RE, "PHONE", out)
        out = _apply(_IPV4_RE, "IPV4", out)
        out = _apply(_IPV6_RE, "IPV6", out)

        return out, summary

    def has_pii(self, text: str) -> bool:
        """Cheap precheck — short-circuit on first hit."""
        if not text:
            return False
        if any(p.search(text) for p in (_EMAIL_RE, _SSN_RE, _PHONE_RE,
                                          _IPV4_RE, _IPV6_RE)):
            return True
        if any(p.search(text) for p, _ in _API_KEY_RES):
            return True
        return any(_luhn_ok(m.group(0)) for m in _CC_RE.finditer(text))

    # --- helpers ---
    def _sub(self, kind: str):
        token = self.placeholder_format.format(kind=kind)
        def _replacer(_m: re.Match) -> str:
            return token
        return _replacer


# Module-level default — share a single instance for callers that don't
# need custom configuration. Stateless, so safe to reuse.
default_redactor = PiiRedactor()


def redact(text: str) -> tuple[str, RedactionSummary]:
    """Convenience for the common case — redact via the default redactor."""
    return default_redactor.redact(text)
