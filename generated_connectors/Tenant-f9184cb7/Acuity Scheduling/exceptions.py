from __future__ import annotations


class AcuityError(Exception):
    """Base exception for all Acuity Scheduling connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class AcuityAuthError(AcuityError):
    """Raised when Acuity Scheduling rejects the credentials (401/403)."""


class AcuityRateLimitError(AcuityError):
    """Raised on 429 Too Many Requests from Acuity Scheduling."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class AcuityNotFoundError(AcuityError):
    """Raised when a requested Acuity Scheduling resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str | int) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class AcuityNetworkError(AcuityError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""
