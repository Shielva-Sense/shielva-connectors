from __future__ import annotations


class AsanaError(Exception):
    """Base exception for all Asana connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class AsanaAuthError(AsanaError):
    """Raised when Asana rejects the access token (401/403)."""


class AsanaRateLimitError(AsanaError):
    """Raised on 429 Too Many Requests from Asana."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class AsanaNotFoundError(AsanaError):
    """Raised when a requested Asana resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class AsanaNetworkError(AsanaError):
    """Raised on transient network failures, timeouts, or 5xx responses."""
