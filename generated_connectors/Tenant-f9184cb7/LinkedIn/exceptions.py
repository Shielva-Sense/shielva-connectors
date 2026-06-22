from __future__ import annotations


class LinkedInError(Exception):
    """Base exception for all LinkedIn connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class LinkedInAuthError(LinkedInError):
    """Raised when LinkedIn rejects the token (401/403)."""


class LinkedInRateLimitError(LinkedInError):
    """Raised on 429 Too Many Requests from LinkedIn."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class LinkedInNotFoundError(LinkedInError):
    """Raised when a requested LinkedIn resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class LinkedInNetworkError(LinkedInError):
    """Raised on transient network failures (timeouts, connection errors)."""


class LinkedInServerError(LinkedInError):
    """Raised on 5xx responses from LinkedIn."""
