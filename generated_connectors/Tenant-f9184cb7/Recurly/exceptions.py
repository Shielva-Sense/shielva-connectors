from __future__ import annotations


class RecurlyError(Exception):
    """Base exception for all Recurly connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class RecurlyAuthError(RecurlyError):
    """Raised when Recurly rejects the API key (401/403)."""


class RecurlyRateLimitError(RecurlyError):
    """Raised on 429 Too Many Requests from Recurly."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class RecurlyNotFoundError(RecurlyError):
    """Raised when a requested Recurly resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str = "") -> None:
        msg = f"{resource} '{resource_id}' not found" if resource_id else f"{resource} not found"
        super().__init__(msg, status_code=404, code="not_found")


class RecurlyNetworkError(RecurlyError):
    """Raised on transient network failures, timeouts, or 5xx responses."""
