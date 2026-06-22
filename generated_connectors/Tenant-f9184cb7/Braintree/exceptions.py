from __future__ import annotations


class BraintreeError(Exception):
    """Base exception for all Braintree connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class BraintreeAuthError(BraintreeError):
    """Raised when Braintree rejects credentials (401/403)."""


class BraintreeNetworkError(BraintreeError):
    """Raised on transient network failures (timeouts, connection errors)."""


class BraintreeNotFoundError(BraintreeError):
    """Raised when a requested Braintree resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="not_found",
        )
        self.resource = resource
        self.resource_id = resource_id


class BraintreeRateLimitError(BraintreeError):
    """Raised on 429 Too Many Requests from Braintree."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after
