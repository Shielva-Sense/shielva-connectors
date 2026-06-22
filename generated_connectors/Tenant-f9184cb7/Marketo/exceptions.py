from __future__ import annotations


class MarketoError(Exception):
    """Base exception for all Marketo connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class MarketoAuthError(MarketoError):
    """Raised when Marketo rejects credentials (401/603/600)."""


class MarketoRateLimitError(MarketoError):
    """Raised on 429 or Marketo error code 606 (Rate limit exceeded)."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class MarketoNotFoundError(MarketoError):
    """Raised when a requested Marketo resource does not exist (404 / code 702)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class MarketoNetworkError(MarketoError):
    """Raised on transient network failures (timeouts, connection errors)."""
