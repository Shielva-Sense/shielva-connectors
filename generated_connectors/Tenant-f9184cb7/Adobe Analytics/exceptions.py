"""Exception hierarchy for the Adobe Analytics connector."""
from __future__ import annotations


class AdobeAnalyticsError(Exception):
    """Base exception for all Adobe Analytics connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class AdobeAnalyticsAuthError(AdobeAnalyticsError):
    """Raised when Adobe IMS or the Analytics API rejects credentials (401/403)."""


class AdobeAnalyticsNetworkError(AdobeAnalyticsError):
    """Raised on transient network failures (timeouts, connection errors)."""


class AdobeAnalyticsNotFoundError(AdobeAnalyticsError):
    """Raised when a requested Adobe Analytics resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class AdobeAnalyticsRateLimitError(AdobeAnalyticsError):
    """Raised on 429 Too Many Requests from Adobe Analytics."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after
