from __future__ import annotations


class ClearbitError(Exception):
    """Base exception for all Clearbit connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class ClearbitAuthError(ClearbitError):
    """Raised when Clearbit rejects the API key (401/403)."""


class ClearbitNetworkError(ClearbitError):
    """Raised on transient network failures (timeouts, connection errors)."""


class ClearbitNotFoundError(ClearbitError):
    """Raised when the requested resource does not exist (404)."""

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            f"{resource} '{identifier}' not found",
            status_code=404,
            code="resource_missing",
        )
        self.resource = resource
        self.identifier = identifier


class ClearbitRateLimitError(ClearbitError):
    """Raised on 429 Too Many Requests from Clearbit."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after
