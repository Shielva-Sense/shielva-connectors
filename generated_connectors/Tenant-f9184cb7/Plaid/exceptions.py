from __future__ import annotations


class PlaidError(Exception):
    """Base exception for all Plaid connector errors."""

    def __init__(self, message: str, status_code: int = 0, error_code: str = "", error_type: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.error_type = error_type


class PlaidAuthError(PlaidError):
    """Raised when Plaid rejects credentials (INVALID_API_KEYS / INVALID_ACCESS_TOKEN)."""


class PlaidItemError(PlaidError):
    """Raised for ITEM_ERROR responses — item needs user re-authentication."""


class PlaidRateLimitError(PlaidError):
    """Raised on RATE_LIMIT_EXCEEDED from Plaid."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, error_code="RATE_LIMIT_EXCEEDED", error_type="RATE_LIMIT_ERROR")
        self.retry_after = retry_after


class PlaidNotFoundError(PlaidError):
    """Raised when a requested Plaid resource does not exist."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            error_code="NOT_FOUND",
        )


class PlaidNetworkError(PlaidError):
    """Raised on transient network failures (timeouts, connection errors)."""
