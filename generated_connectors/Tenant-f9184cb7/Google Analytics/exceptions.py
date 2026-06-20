"""Exception hierarchy for the Google Analytics 4 connector."""
from __future__ import annotations


class GoogleAnalyticsError(Exception):
    """Base exception for all Google Analytics connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class GoogleAnalyticsAuthError(GoogleAnalyticsError):
    """Raised when the GA4 API rejects credentials (401/403)."""


class GoogleAnalyticsNetworkError(GoogleAnalyticsError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""


class GoogleAnalyticsNotFoundError(GoogleAnalyticsError):
    """Raised when a requested GA4 resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class GoogleAnalyticsRateLimitError(GoogleAnalyticsError):
    """Raised on 429 Too Many Requests from the GA4 API."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after
