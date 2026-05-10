"""Centralized logging setup.

Honors LOG_LEVEL and LOG_FORMAT env vars. JSON format is selected when
LOG_FORMAT=json, otherwise plain text. Call `configure_logging()` once at
application startup; module-level loggers obtained via `get_logger(__name__)`
inherit the configuration.

PII scrubbing
-------------
``_PiiScrubFilter`` runs every log record's formatted message + ``ctx_*``
extras through ``src.pii.default_redactor``. Emails, phone numbers, SSNs,
Luhn-validated credit cards, IPs and common API-key shapes are replaced
with ``<REDACTED:KIND>`` placeholders before the line ever leaves the
process. Toggleable via the ``observability.pii_scrub_logs`` runtime
setting (default on); turn off only for narrow debugging windows.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any


class _PiiScrubFilter(logging.Filter):
    """Redact PII from every log record before any formatter sees it.

    Sits as a logger-level Filter (not a Formatter wrapper) so it runs
    once per record regardless of which handler emits. The filter is
    a no-op if ``observability.pii_scrub_logs`` is off — checked
    defensively per-call so an admin toggle takes effect without a
    process restart.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._enabled():
            return True
        try:
            from .pii import default_redactor
        except Exception:  # noqa: BLE001 — bootstrap order guard
            return True

        # Redact the formatted message AND any ctx_* extras whose values
        # are user-supplied strings (request paths, query strings, etc.).
        # Numeric ctx_* values pass through.
        try:
            msg = record.getMessage()
            redacted, _ = default_redactor.redact(msg)
            if redacted != msg:
                # Replace the message + clear the args so getMessage()
                # returns the redacted form on subsequent calls.
                record.msg = redacted
                record.args = None
        except Exception:  # noqa: BLE001 — never let logging break the app
            pass

        for key in list(record.__dict__):
            if not key.startswith("ctx_"):
                continue
            value = getattr(record, key, None)
            if isinstance(value, str):
                try:
                    new_value, _ = default_redactor.redact(value)
                    if new_value != value:
                        setattr(record, key, new_value)
                except Exception:  # noqa: BLE001
                    pass
        return True

    @staticmethod
    def _enabled() -> bool:
        try:
            from .runtime_settings import get_runtime
            return bool(get_runtime().get("observability.pii_scrub_logs", True))
        except Exception:  # noqa: BLE001 — bootstrap order
            return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key.startswith("ctx_"):
                payload[key[4:]] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Configure root logger. Idempotent."""
    level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    fmt = (fmt or os.environ.get("LOG_FORMAT", "text")).lower()

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)-7s] %(name)s :: %(message)s",
            datefmt="%H:%M:%S",
        ))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # PII scrub runs on the root so every emitter inherits — the filter
    # is process-global and stateless, safe to attach unconditionally.
    if not any(isinstance(f, _PiiScrubFilter) for f in root.filters):
        root.addFilter(_PiiScrubFilter())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
