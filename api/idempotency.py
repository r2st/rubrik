"""Idempotency-Key middleware (research §"Backpressure must be explicit").

Without this, a client retry on a POST/PUT/PATCH/DELETE produces a duplicate
write — admin password rotated twice, snapshot rebuild enqueued twice,
audit-log row written twice. The fix is the standard one
(`Idempotency-Key` header convention, RFC 9457-adjacent):

  1. Client sends ``Idempotency-Key: <uuid>`` on a state-changing request.
  2. Server hashes the request (method + path + body) under that key.
  3. If we've already seen ``(key, hash)``, return the cached response.
     The handler is **not** re-executed.
  4. If we've seen the key with a *different* hash, return ``409 Conflict``
     — same key, different intent, almost certainly a client bug.
  5. Otherwise execute as normal and cache the response for ``TTL`` hours.

Storage:
  - Redis when ``[runtime].redis_url`` is set (cluster-wide, the right answer
    in production). Key: ``idempotency:{key}``, value: JSON envelope of
    ``{request_hash, status, headers, body, stored_at}``.
  - In-process fallback (``dict`` + threading lock) — single-replica only,
    keeps the dev workflow working without Redis.

Tunable via the admin panel:
  - ``idempotency.enabled``         bool   default ``false`` (opt-in)
  - ``idempotency.ttl_hours``       int    default ``24``
  - ``idempotency.max_body_bytes``  int    default ``16384``  (skip the
                                          hash for bodies bigger than this;
                                          large uploads are unlikely to be
                                          idempotency-key targets)

Bypass paths: same as backpressure — ``/api/live``, ``/api/ready``,
``/api/health``, ``/metrics``. GET / HEAD are no-ops.

This is the third hardening layer on the write path:
  body-size cap → backpressure → adaptive throttle → per-tenant rate limit
  → strict admin throttle → **idempotency cache** → handler.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.logging_config import get_logger

log = get_logger(__name__)

_BYPASS_PATHS = ("/api/live", "/api/ready", "/api/health", "/metrics")
_STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
_HEADER = "Idempotency-Key"


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Cache responses keyed by ``Idempotency-Key`` to make retries safe."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._local: dict[str, dict] = {}
        self._local_lock = threading.Lock()

    async def dispatch(self, request: Request, call_next):
        if not self._enabled():
            return await call_next(request)
        if request.method not in _STATE_CHANGING:
            return await call_next(request)
        if request.url.path.startswith(_BYPASS_PATHS):
            return await call_next(request)

        key = request.headers.get(_HEADER)
        if not key:
            return await call_next(request)  # opt-in per request, too

        # Body sniff — read it once, replay it for the handler.
        max_body = self._max_body_bytes()
        body = await request.body()
        if len(body) > max_body:
            # Don't try to dedupe huge payloads — pass through unmemoized.
            return await call_next(request)
        request_hash = _hash_request(request, body)

        cached = self._lookup(key)
        if cached is not None:
            if cached["request_hash"] != request_hash:
                log.warning(
                    "Idempotency conflict: key=%s reused with different "
                    "request hash (path=%s)",
                    key, request.url.path,
                )
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": {
                            "code": "idempotency_conflict",
                            "message": (
                                "Idempotency-Key was used previously with a "
                                "different request body. This is almost "
                                "certainly a client bug."
                            ),
                            "request_id": getattr(
                                request.state, "request_id", None,
                            ),
                            "path": str(request.url.path),
                        }
                    },
                )
            log.info("Idempotency hit: key=%s path=%s", key, request.url.path)
            return _replay(cached)

        # Miss — execute and cache. Replay the body since we already read it.
        async def _receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = _receive  # noqa: SLF001

        response = await call_next(request)
        envelope = await _envelope(response, request_hash)
        self._store(key, envelope)
        # Mark the response so consumers can see it was just-cached.
        response.headers["Idempotency-Status"] = "stored"
        return response

    # ----- storage -------------------------------------------------------
    def _lookup(self, key: str) -> Optional[dict]:
        url = _redis_url()
        if url:
            try:
                import redis
                client = redis.Redis.from_url(url, socket_timeout=0.25)
                raw = client.get(f"idempotency:{key}")
                if raw is None:
                    return None
                return json.loads(raw)
            except Exception:  # noqa: BLE001
                pass  # fall through to local
        with self._local_lock:
            entry = self._local.get(key)
            if entry is None:
                return None
            if entry["expires_at"] < time.time():
                self._local.pop(key, None)
                return None
            return entry

    def _store(self, key: str, envelope: dict) -> None:
        ttl_seconds = self._ttl_hours() * 3600
        envelope["expires_at"] = time.time() + ttl_seconds
        url = _redis_url()
        if url:
            try:
                import redis
                client = redis.Redis.from_url(url, socket_timeout=0.25)
                client.setex(
                    f"idempotency:{key}", ttl_seconds, json.dumps(envelope),
                )
                return
            except Exception:  # noqa: BLE001
                pass
        with self._local_lock:
            self._local[key] = envelope

    # ----- runtime-tunable knobs ----------------------------------------
    @staticmethod
    def _enabled() -> bool:
        try:
            from src.runtime_settings import get_runtime
            return bool(get_runtime().get("idempotency.enabled", False))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _ttl_hours() -> int:
        try:
            from src.runtime_settings import get_runtime
            return int(get_runtime().get("idempotency.ttl_hours", 24))
        except Exception:  # noqa: BLE001
            return 24

    @staticmethod
    def _max_body_bytes() -> int:
        try:
            from src.runtime_settings import get_runtime
            return int(get_runtime().get("idempotency.max_body_bytes", 16384))
        except Exception:  # noqa: BLE001
            return 16384


def _hash_request(request: Request, body: bytes) -> str:
    """SHA-256 over ``method | path | body``. Headers excluded so legitimate
    proxy retries (different ``X-Request-ID``, etc.) still match."""
    h = hashlib.sha256()
    h.update(request.method.encode("utf-8"))
    h.update(b"|")
    h.update(str(request.url.path).encode("utf-8"))
    h.update(b"|")
    h.update(body or b"")
    return h.hexdigest()


async def _envelope(response: Response, request_hash: str) -> dict:
    """Read the response body once and return a serialisable envelope."""
    body_chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        body_chunks.append(chunk)
    body = b"".join(body_chunks)

    # Replace the iterator so the response can still be sent to the client.
    async def _replay_iter():
        yield body
    response.body_iterator = _replay_iter()

    return {
        "request_hash": request_hash,
        "status_code": response.status_code,
        "headers": [
            (k, v) for k, v in response.headers.items()
            if k.lower() not in ("content-length", "content-encoding")
        ],
        "body": body.decode("utf-8", errors="replace"),
        "stored_at": time.time(),
    }


def _replay(envelope: dict) -> Response:
    """Reconstruct a Response from a cached envelope."""
    headers = dict(envelope["headers"])
    headers["Idempotency-Status"] = "replayed"
    return Response(
        content=envelope["body"],
        status_code=envelope["status_code"],
        headers=headers,
    )


def _redis_url() -> Optional[str]:
    try:
        from src.settings import get_settings
        return get_settings().redis_url
    except Exception:  # noqa: BLE001
        return None
