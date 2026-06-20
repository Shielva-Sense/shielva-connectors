from __future__ import annotations


class AhaError(Exception):
    """Base exception for all Aha! connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class AhaAuthError(AhaError):
    """Raised when the Aha! API rejects the API key (401/403)."""


class AhaRateLimitError(AhaError):
    """Raised on 429 Too Many Requests from Aha!."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class AhaNotFoundError(AhaError):
    """Raised when a requested Aha! resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class AhaNetworkError(AhaError):
    """Raised on transient network failures, timeouts, or 5xx responses."""
