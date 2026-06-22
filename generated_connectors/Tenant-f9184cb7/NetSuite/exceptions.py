from __future__ import annotations


class NetSuiteError(Exception):
    """Base exception for all NetSuite connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class NetSuiteAuthError(NetSuiteError):
    """Raised when NetSuite rejects credentials or the OAuth 1.0a signature (401/403)."""


class NetSuiteRateLimitError(NetSuiteError):
    """Raised on 429 Too Many Requests from the NetSuite REST API."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class NetSuiteNotFoundError(NetSuiteError):
    """Raised when a requested NetSuite resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class NetSuiteNetworkError(NetSuiteError):
    """Raised on transient network failures (timeouts, connection errors)."""


class NetSuiteServerError(NetSuiteError):
    """Raised on 5xx responses from the NetSuite REST API."""


class NetSuiteValidationError(NetSuiteError):
    """Raised when install_fields are missing or malformed before a request is sent."""
