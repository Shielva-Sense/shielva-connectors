from __future__ import annotations


class GreenhouseError(Exception):
    """Base exception for all Greenhouse connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class GreenhouseAuthError(GreenhouseError):
    """Raised when Greenhouse rejects the API key (401/403)."""


class GreenhouseNetworkError(GreenhouseError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""


class GreenhouseRateLimitError(GreenhouseError):
    """Raised on 429 Too Many Requests from the Greenhouse Harvest API."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class GreenhouseNotFoundError(GreenhouseError):
    """Raised when a requested resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="not_found",
        )
