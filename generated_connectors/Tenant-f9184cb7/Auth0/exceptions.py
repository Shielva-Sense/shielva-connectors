"""Auth0 connector exception hierarchy."""

from __future__ import annotations


class Auth0Error(Exception):
    """Base exception for all Auth0 connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class Auth0AuthError(Auth0Error):
    """Raised when Auth0 rejects the management token (401/403)."""


class Auth0RateLimitError(Auth0Error):
    """Raised on 429 Too Many Requests from the Auth0 Management API."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class Auth0NotFoundError(Auth0Error):
    """Raised when a requested Auth0 resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class Auth0NetworkError(Auth0Error):
    """Raised on transient network failures (timeouts, connection errors)."""
