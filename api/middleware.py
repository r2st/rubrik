"""Cross-cutting middleware: request IDs, security headers, state-age tracking."""
from __future__ import annotations

import time
import uuid

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.logging_config import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Body size limit — DoS prevention
# ---------------------------------------------------------------------------
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB — generous for JSON, blocks abuse


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds `max_bytes`.

    FastAPI / Starlette default is unbounded — that's a real DoS vector
    (megabyte-then-gigabyte upload would happily fill the request buffer).
    Enforce a hard cap before any handler runs. The limit is generous (1 MiB)
    for typical JSON payloads but cheaply rejects abuse.

    `Content-Length` is checked first (rejects fast on the prelude). If the
    header is absent, we read the body in a streaming fashion and abort if
    we cross the threshold.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > self.max_bytes:
                    return self._too_large(request)
            except ValueError:
                # Malformed header — let the framework reject it
                pass

        # Some clients omit Content-Length (chunked transfer). Wrap receive()
        # to enforce the cap incrementally.
        original_receive = request.receive
        bytes_seen = 0

        async def capped_receive():
            nonlocal bytes_seen
            message = await original_receive()
            if message.get("type") == "http.request":
                body = message.get("body") or b""
                bytes_seen += len(body)
                if bytes_seen > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        request._receive = capped_receive  # noqa: SLF001
        try:
            return await call_next(request)
        except _BodyTooLarge:
            return self._too_large(request)

    def _too_large(self, request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "request_too_large",
                    "message": f"Request body exceeds {self.max_bytes} bytes",
                    "request_id": getattr(request.state, "request_id", None),
                    "path": str(request.url.path),
                }
            },
        )


class _BodyTooLarge(Exception):
    """Raised internally by BodySizeLimitMiddleware when streaming the body
    crosses the configured cap."""


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


# ---------------------------------------------------------------------------
# State-age / graceful degradation
# ---------------------------------------------------------------------------
class StateAgeMiddleware(BaseHTTPMiddleware):
    """Stamps every /api/* response with how old the cached pipeline state is.

    - X-State-Age-Seconds: integer seconds since the last successful build.
    - X-Stale-Response: "true" iff pipeline refresh has been failing long enough
      that the data is older than 2× the configured refresh interval.

    Refresh failures don't translate to 5xx — clients get the last-good data
    plus an honest staleness signal. They can choose what to do with it
    (warn the user, retry, fail closed for sensitive operations).
    """

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        # Only annotate API responses; static assets don't need this.
        if request.url.path.startswith("/api/"):
            from . import state  # local import to avoid circular at module load
            response.headers["X-State-Age-Seconds"] = str(state.state_age_seconds())
            if state.is_stale():
                response.headers["X-Stale-Response"] = "true"
        return response
