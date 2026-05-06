"""HTTP caching for read endpoints — ETag + Cache-Control + 304 Not Modified.

The pipeline state changes rarely (only when `state.reload()` runs, which is
either on startup or every `PIPELINE_REFRESH_MINUTES`). We tie ETags to the
state's identity so clients can revalidate cheaply.

Strategy:
  - Compute a single weak ETag per pipeline build (a hash of the metadata)
  - Tag every cached response with that ETag + a short Cache-Control max-age
  - On If-None-Match match, return 304 with no body — the dashboard's repeat
    loads cost ~zero bandwidth

Use:
    @router.get("/summary")
    def summary(request: Request, response: Response) -> SummaryResponse:
        return cached(request, response, build_payload())
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Request, Response

from .state import get_state


def _etag_for_state() -> str:
    """Stable ETag for the current pipeline build.

    Hashes the metadata (which captures n_meetings, date range, k, silhouette).
    A new build → new ETag → cache miss. Format: weak ETag (W/"...") so we
    don't claim byte-for-byte identity.
    """
    s = get_state()
    canonical = json.dumps(s.metadata, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f'W/"{digest}"'


def cached(request: Request, response: Response, payload: Any,
           *, max_age: int = 60) -> Any:
    """Stamp ETag + Cache-Control on a response, honor If-None-Match.

    On cache hit, returns a 304 Response directly (no body, ETag preserved).
    Returning a Response from a FastAPI route bypasses response_model
    validation — exactly what we want for 304.

    Args:
        request: incoming request (read If-None-Match)
        response: outgoing response (set ETag + Cache-Control on 200 path)
        payload: the body to return on cache miss
        max_age: Cache-Control max-age in seconds (default 60s)

    Returns:
        `payload` on cache miss, or a 304 Response on cache hit.
    """
    etag = _etag_for_state()
    cache_control = f"private, max-age={max_age}, must-revalidate"

    inbound = request.headers.get("if-none-match")
    if inbound and inbound == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = cache_control
    return payload
