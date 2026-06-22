from __future__ import annotations


class XeroError(Exception):
    """Base exception for all Xero connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class XeroAuthError(XeroError):
    """Raised when Xero rejects the OAuth2 token (401/403)."""


class XeroRateLimitError(XeroError):
    """Raised on 429 Too Many Requests from Xero."""

    def __init__(self, message: str, retry_after: float = 60.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class XeroNotFoundError(XeroError):
    """Raised when a requested Xero resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="not_found",
        )


class XeroNetworkError(XeroError):
    """Raised on transient network failures (timeouts, connection errors)."""
