"""Standardized error envelope.

Every error response — whether HTTPException, validation error, or unhandled
exception — comes back in the same shape:

    {
      "error": {
        "code": "not_found",
        "message": "meeting X does not exist",
        "request_id": "01J…",
        "path": "/api/v1/meetings/X"
      }
    }

Stable contract for clients; no leaking framework internals.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.logging_config import get_logger

log = get_logger(__name__)


class ErrorBody(BaseModel):
    code: str = Field(description="Machine-readable error code (snake_case).")
    message: str = Field(description="Human-readable description.")
    request_id: Optional[str] = Field(default=None, description="Correlation ID.")
    path: Optional[str] = Field(default=None, description="Request path that failed.")
    details: Optional[List[dict]] = Field(
        default=None,
        description="Field-level errors for validation failures.",
    )


class ErrorResponse(BaseModel):
    error: ErrorBody


# ---------------------------------------------------------------------------
# Status code → error code mapping
# ---------------------------------------------------------------------------
_CODE_FOR_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_failed",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


def _request_id(request: Request) -> Optional[str]:
    return getattr(request.state, "request_id", None)


def _envelope(
    status_code: int,
    message: str,
    request: Request,
    *,
    code: Optional[str] = None,
    details: Optional[List[dict]] = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code or _CODE_FOR_STATUS.get(status_code, "error"),
            message=message,
            request_id=_request_id(request),
            path=str(request.url.path),
            details=details,
        )
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(exclude_none=True))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _envelope(exc.status_code, str(exc.detail), request)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return _envelope(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "request validation failed",
        request,
        details=[
            {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ],
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled exception", extra={"ctx_path": str(request.url.path)})
    return _envelope(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "internal server error",
        request,
    )


def register(app: FastAPI) -> None:
    """Wire all three handlers into the app."""
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
