from __future__ import annotations


class FreshworksCRMError(Exception):
    """Base exception for all Freshworks CRM connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class FreshworksCRMAuthError(FreshworksCRMError):
    """Raised when Freshworks CRM rejects the API key (401/403)."""


class FreshworksCRMRateLimitError(FreshworksCRMError):
    """Raised on 429 Too Many Requests from Freshworks CRM."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class FreshworksCRMNotFoundError(FreshworksCRMError):
    """Raised when a requested Freshworks CRM resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class FreshworksCRMNetworkError(FreshworksCRMError):
    """Raised on transient network failures, timeouts, or 5xx responses."""
