from __future__ import annotations


class FreshserviceError(Exception):
    """Base exception for all Freshservice connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class FreshserviceAuthError(FreshserviceError):
    """Raised when Freshservice rejects the API key (401/403)."""


class FreshserviceRateLimitError(FreshserviceError):
    """Raised on 429 Too Many Requests from Freshservice."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class FreshserviceNotFoundError(FreshserviceError):
    """Raised when a requested Freshservice resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class FreshserviceNetworkError(FreshserviceError):
    """Raised on transient network failures, timeouts, or 5xx responses."""
