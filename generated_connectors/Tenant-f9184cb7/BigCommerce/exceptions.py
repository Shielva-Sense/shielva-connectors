from __future__ import annotations


class BigCommerceError(Exception):
    """Base exception for all BigCommerce connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class BigCommerceAuthError(BigCommerceError):
    """Raised when BigCommerce rejects the access token (401/403)."""


class BigCommerceNetworkError(BigCommerceError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""


class BigCommerceRateLimitError(BigCommerceError):
    """Raised on 429 Too Many Requests from BigCommerce."""

    def __init__(self, message: str, retry_after: float = 2.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class BigCommerceNotFoundError(BigCommerceError):
    """Raised when a requested BigCommerce resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="not_found",
        )
