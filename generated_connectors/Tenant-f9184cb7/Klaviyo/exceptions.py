from __future__ import annotations


class KlaviyoError(Exception):
    """Base exception for all Klaviyo connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class KlaviyoAuthError(KlaviyoError):
    """Raised when Klaviyo rejects the API key (401/403)."""


class KlaviyoInvalidKeyError(KlaviyoAuthError):
    """Raised when the API key format is invalid or does not start with 'pk_'."""


class KlaviyoRateLimitError(KlaviyoError):
    """Raised on 429 Too Many Requests from Klaviyo."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class KlaviyoNotFoundError(KlaviyoError):
    """Raised when a requested Klaviyo resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class KlaviyoNetworkError(KlaviyoError):
    """Raised on transient network failures (timeouts, connection errors)."""


class KlaviyoServerError(KlaviyoError):
    """Raised on 5xx responses from Klaviyo."""
