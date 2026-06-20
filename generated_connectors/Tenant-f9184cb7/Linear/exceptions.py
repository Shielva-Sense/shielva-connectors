from __future__ import annotations


class LinearError(Exception):
    """Base exception for all Linear connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class LinearAuthError(LinearError):
    """Raised when Linear rejects the credentials (401/403 or GraphQL UNAUTHORIZED)."""


class LinearRateLimitError(LinearError):
    """Raised on 429 Too Many Requests from Linear."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class LinearNotFoundError(LinearError):
    """Raised when a requested Linear resource does not exist (404 or not-found GraphQL error)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class LinearNetworkError(LinearError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""
