"""Cross-cutting exception hierarchy for the Shielva Integration Builder.

Three concrete subtypes map cleanly to HTTP status codes and caller expectations:

* IntegrationException (502, retryable=True)  — upstream dependency failure
  (LLM API, R2, MongoDB, Connector Gateway, GitHub).
* RuntimeException (400, retryable=False)     — caller / input error
  (invalid connector spec, missing field, unsupported provider).
* TechnicalException (500, retryable=False)   — internal implementation error
  (unexpected code path, assertion failed).

All subclass ShielvaException so a single ``except ShielvaException`` clause
at the boundary catches everything and the ``install_exception_handlers``
FastAPI handler converts it to a structured JSON response.
"""

from __future__ import annotations


class ShielvaException(Exception):
    """Base exception for all Shielva Integration Builder errors."""

    status_code: int = 500
    retryable: bool = False
    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}(message={self.message!r}, error_code={self.error_code!r})"


class IntegrationException(ShielvaException):
    """Upstream dependency failure — retryable.

    Raised when an external service (LLM API, R2, MongoDB, Connector Gateway,
    GitHub) is unavailable or returns an unexpected error.
    """

    status_code = 502
    retryable = True
    error_code = "INTEGRATION_ERROR"


class RuntimeException(ShielvaException):
    """Caller/input error — non-retryable.

    Raised when the request is structurally invalid or references a resource
    that does not exist.  HTTP 400 range.
    """

    status_code = 400
    retryable = False
    error_code = "RUNTIME_ERROR"


class NotFoundException(RuntimeException):
    """Resource not found — non-retryable (404)."""

    status_code = 404
    error_code = "NOT_FOUND"


class ConflictException(RuntimeException):
    """Conflict with existing resource state — non-retryable (409)."""

    status_code = 409
    error_code = "CONFLICT"


class TechnicalException(ShielvaException):
    """Internal implementation error — non-retryable.

    Raised for unexpected code paths, assertion failures, or any error that
    indicates a bug in the service itself.
    """

    status_code = 500
    retryable = False
    error_code = "TECHNICAL_ERROR"
