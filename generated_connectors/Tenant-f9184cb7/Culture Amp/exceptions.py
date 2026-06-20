from __future__ import annotations


class CultureAmpError(Exception):
    """Base exception for all Culture Amp connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class CultureAmpAuthError(CultureAmpError):
    """Raised when Culture Amp rejects the credentials (401/403)."""


class CultureAmpRateLimitError(CultureAmpError):
    """Raised on 429 Too Many Requests from Culture Amp."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class CultureAmpNotFoundError(CultureAmpError):
    """Raised when a requested Culture Amp resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str | int) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class CultureAmpNetworkError(CultureAmpError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""
