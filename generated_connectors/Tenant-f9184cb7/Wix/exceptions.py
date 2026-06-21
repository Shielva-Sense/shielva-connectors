"""Custom exceptions for the Wix connector."""


class WixError(Exception):
    """Base exception for all Wix connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class WixAuthError(WixError):
    """Raised when authentication fails or the api_key is invalid (401/403)."""


class WixNetworkError(WixError):
    """Raised on transport-level failures (timeouts, connection errors, 5xx after retries)."""


class WixNotFound(WixError):
    """Raised when a resource is not found (404)."""
