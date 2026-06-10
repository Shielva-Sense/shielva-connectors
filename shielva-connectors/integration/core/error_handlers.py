"""Global FastAPI exception handlers for the Integration Builder.

All four handlers produce a uniform JSON envelope::

    {
      "error": {
        "code":      "RUNTIME_ERROR",
        "message":   "Human-readable description",
        "retryable": false,
        "detail":    "Optional extra context"  # omitted when None
      }
    }

Wire them in with ``install_exception_handlers(app)`` after ``app = FastAPI(...)``.

SOC 2 C1.1 note: the generic Exception handler returns a static "internal error"
string — raw exception messages are never forwarded to clients.
"""
from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from integration.core.errors import ShielvaException

logger = structlog.get_logger(__name__)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    detail: str | None = None,
    retryable: bool = False,
) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message, "retryable": retryable}}
    if detail is not None:
        body["error"]["detail"] = detail
    return JSONResponse(status_code=status_code, content=body)


def install_exception_handlers(app: FastAPI) -> None:
    """Register all four exception handlers on *app*."""

    @app.exception_handler(ShielvaException)
    async def _shielva_handler(request: Request, exc: ShielvaException) -> JSONResponse:
        logger.warning(
            "shielva_exception",
            error_code=exc.error_code,
            message=exc.message,
            path=str(request.url.path),
            retryable=exc.retryable,
        )
        return _error_response(
            exc.status_code,
            exc.error_code,
            exc.message,
            detail=exc.detail,
            retryable=exc.retryable,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        logger.info(
            "http_exception",
            status_code=exc.status_code,
            detail=str(exc.detail),
            path=str(request.url.path),
        )
        return _error_response(exc.status_code, "HTTP_ERROR", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.info(
            "validation_error",
            path=str(request.url.path),
            error_count=len(exc.errors()),
        )
        return _error_response(
            422,
            "VALIDATION_ERROR",
            "Request validation failed",
            detail=str(exc.errors()),
        )

    @app.exception_handler(Exception)
    async def _generic_handler(request: Request, exc: Exception) -> JSONResponse:
        # SOC 2 C1.1 — never forward raw exception detail to clients.
        logger.exception(
            "unhandled_exception",
            exc_type=type(exc).__name__,
            path=str(request.url.path),
        )
        return _error_response(500, "INTERNAL_ERROR", "An internal error occurred.")
