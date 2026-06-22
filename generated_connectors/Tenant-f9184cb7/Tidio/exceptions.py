from __future__ import annotations


class TidioError(Exception):
    """Base exception for all Tidio connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class TidioAuthError(TidioError):
    """Raised when Tidio rejects the credentials (401/403)."""


class TidioRateLimitError(TidioError):
    """Raised on 429 Too Many Requests from Tidio."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class TidioNotFoundError(TidioError):
    """Raised when a requested Tidio resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str | int) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class TidioNetworkError(TidioError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""
