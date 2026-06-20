from __future__ import annotations


class CustomerIOError(Exception):
    """Base exception for all Customer.io connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class CustomerIOAuthError(CustomerIOError):
    """Raised when Customer.io rejects the App API key (401/403)."""


class CustomerIOInvalidKeyError(CustomerIOAuthError):
    """Raised when the App API key format is invalid."""


class CustomerIORateLimitError(CustomerIOError):
    """Raised on 429 Too Many Requests from Customer.io."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class CustomerIONotFoundError(CustomerIOError):
    """Raised when a requested Customer.io resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class CustomerIONetworkError(CustomerIOError):
    """Raised on transient network failures (timeouts, connection errors)."""


class CustomerIOServerError(CustomerIOError):
    """Raised on 5xx responses from Customer.io."""
