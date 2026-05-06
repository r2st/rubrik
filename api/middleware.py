"""Cross-cutting middleware: request IDs and security headers."""
from __future__ import annotations

import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.logging_config import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Request ID — correlation across logs, responses, and traces
# ---------------------------------------------------------------------------
class RequestIDMiddleware(BaseHTTPMiddleware):
    """Stamp every request with a unique ID; thread it through logs and responses.

    Honors an inbound `X-Request-ID` if a load balancer / mesh has already
    minted one (so traces correlate end-to-end). Otherwise mints a UUID4.
    """

    HEADER = "X-Request-ID"

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get(self.HEADER) or uuid.uuid4().hex
        request.state.request_id = rid
        start = time.perf_counter()

        try:
            response: Response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            log.exception(
                "request failed",
                extra={
                    "ctx_request_id": rid,
                    "ctx_method": request.method,
                    "ctx_path": str(request.url.path),
                    "ctx_elapsed_ms": round(elapsed_ms, 2),
                },
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers[self.HEADER] = rid

        # Single structured access-log line per request — useful in JSON-format mode
        log.info(
            "%s %s -> %d (%.0fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            extra={
                "ctx_request_id": rid,
                "ctx_method": request.method,
                "ctx_path": str(request.url.path),
                "ctx_status": response.status_code,
                "ctx_elapsed_ms": round(elapsed_ms, 2),
            },
        )
        return response


# ---------------------------------------------------------------------------
# Security headers — defense in depth, free
# ---------------------------------------------------------------------------
_DEFAULT_CSP = (
    "default-src 'self'; "
    # Plotly + Mermaid load from these CDNs
    "script-src 'self' 'unsafe-inline' https://cdn.plot.ly https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds standard hardening headers to every response.

    These cost nothing and close several common attack vectors:
      X-Content-Type-Options : prevents MIME-sniffing (XSS / CSV-as-HTML)
      X-Frame-Options        : prevents clickjacking
      Referrer-Policy        : strips referrer leakage to third parties
      Strict-Transport-Security : enforces HTTPS once seen (prod only — TLS-terminated)
      Content-Security-Policy: locks down what scripts/origins can load
      Permissions-Policy     : disables sensors we don't use
    """

    def __init__(self, app: ASGIApp, hsts: bool = False, csp: str = _DEFAULT_CSP) -> None:
        super().__init__(app)
        self._hsts = hsts
        self._csp = csp

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault("Content-Security-Policy", self._csp)
        if self._hsts:
            h.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response
