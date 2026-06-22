from __future__ import annotations


class FifteenFiveError(Exception):
    """Base exception for all 15Five connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class FifteenFiveAuthError(FifteenFiveError):
    """Raised when 15Five rejects the credentials (401/403)."""


class FifteenFiveRateLimitError(FifteenFiveError):
    """Raised on 429 Too Many Requests from 15Five."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class FifteenFiveNotFoundError(FifteenFiveError):
    """Raised when a requested 15Five resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str | int) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class FifteenFiveNetworkError(FifteenFiveError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""
