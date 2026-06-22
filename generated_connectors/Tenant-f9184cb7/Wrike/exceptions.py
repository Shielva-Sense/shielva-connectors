"""Wrike connector exception hierarchy."""
from __future__ import annotations


class WrikeError(Exception):
    """Base exception for all Wrike connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"status_code={self.status_code}, "
            f"code={self.code!r})"
        )


class WrikeAuthError(WrikeError):
    """Raised when Wrike rejects the access token (401/403)."""


class WrikeRateLimitError(WrikeError):
    """Raised on 429 Too Many Requests from Wrike."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class WrikeNotFoundError(WrikeError):
    """Raised when a requested Wrike resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )
        self.resource = resource
        self.resource_id = resource_id


class WrikeNetworkError(WrikeError):
    """Raised on transient network failures, timeouts, or 5xx responses."""
