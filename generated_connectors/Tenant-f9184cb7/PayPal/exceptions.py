from __future__ import annotations


class PayPalError(Exception):
    """Base exception for all PayPal connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class PayPalAuthError(PayPalError):
    """Raised when PayPal rejects the client credentials (401/403)."""


class PayPalInvalidCredentialsError(PayPalAuthError):
    """Raised when the client_id or client_secret is invalid or explicitly rejected."""


class PayPalRateLimitError(PayPalError):
    """Raised on 429 Too Many Requests from PayPal."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class PayPalNotFoundError(PayPalError):
    """Raised when a requested PayPal resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(f"{resource} '{resource_id}' not found", status_code=404, code="resource_not_found")


class PayPalNetworkError(PayPalError):
    """Raised on transient network failures (timeouts, connection errors)."""


class PayPalServerError(PayPalError):
    """Raised on 5xx responses from PayPal."""


class PayPalTokenError(PayPalAuthError):
    """Raised when token acquisition fails."""
