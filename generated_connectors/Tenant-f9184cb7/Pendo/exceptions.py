from __future__ import annotations


class PendoError(Exception):
    """Base exception for all Pendo connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class PendoAuthError(PendoError):
    """Raised when Pendo rejects the integration key (401/403)."""


class PendoRateLimitError(PendoError):
    """Raised on 429 Too Many Requests from Pendo."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class PendoNotFoundError(PendoError):
    """Raised when a requested Pendo resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class PendoNetworkError(PendoError):
    """Raised on transient network failures (timeouts, connection errors)."""


class PendoServerError(PendoError):
    """Raised on 5xx responses from Pendo."""
