from __future__ import annotations


class NewRelicError(Exception):
    """Base exception for all New Relic connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class NewRelicAuthError(NewRelicError):
    """Raised when New Relic rejects the API key (401/403)."""


class NewRelicNotFoundError(NewRelicError):
    """Raised when a requested New Relic resource does not exist (404)."""

    def __init__(self, resource: str = "", resource_id: str = "") -> None:
        msg = f"{resource} '{resource_id}' not found" if resource else "Resource not found"
        super().__init__(msg, status_code=404, code="resource_missing")


class NewRelicRateLimitError(NewRelicError):
    """Raised on 429 Too Many Requests from New Relic."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class NewRelicNetworkError(NewRelicError):
    """Raised on 5xx responses or transient network failures from New Relic."""
