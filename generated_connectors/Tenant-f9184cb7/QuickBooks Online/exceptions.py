from __future__ import annotations


class QuickBooksError(Exception):
    """Base exception for all QuickBooks connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class QuickBooksAuthError(QuickBooksError):
    """Raised when Intuit rejects credentials or the access token (401/403)."""


class QuickBooksRateLimitError(QuickBooksError):
    """Raised on 429 Too Many Requests from the QBO API."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class QuickBooksNotFoundError(QuickBooksError):
    """Raised when a requested QBO resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class QuickBooksNetworkError(QuickBooksError):
    """Raised on transient network failures (timeouts, connection errors)."""


class QuickBooksServerError(QuickBooksError):
    """Raised on 5xx responses from the QBO API."""
