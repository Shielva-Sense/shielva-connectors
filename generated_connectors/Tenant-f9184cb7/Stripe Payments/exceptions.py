from __future__ import annotations


class StripeError(Exception):
    """Base exception for all Stripe connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class StripeAuthError(StripeError):
    """Raised when Stripe rejects the API key (401/403)."""


class StripeInvalidKeyError(StripeAuthError):
    """Raised when the API key format is invalid or explicitly rejected."""


class StripeRateLimitError(StripeError):
    """Raised on 429 Too Many Requests from Stripe."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class StripeNotFoundError(StripeError):
    """Raised when a requested Stripe resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(f"{resource} '{resource_id}' not found", status_code=404, code="resource_missing")


class StripeNetworkError(StripeError):
    """Raised on transient network failures (timeouts, connection errors)."""


class StripeServerError(StripeError):
    """Raised on 5xx responses from Stripe."""
