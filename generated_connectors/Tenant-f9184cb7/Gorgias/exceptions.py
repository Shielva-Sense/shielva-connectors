from __future__ import annotations


class GorgiasError(Exception):
    """Base exception for all Gorgias connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class GorgiasAuthError(GorgiasError):
    """Raised when Gorgias rejects the credentials (401/403)."""


class GorgiasRateLimitError(GorgiasError):
    """Raised on 429 Too Many Requests from Gorgias."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class GorgiasNotFoundError(GorgiasError):
    """Raised when a requested Gorgias resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str | int) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class GorgiasNetworkError(GorgiasError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""
