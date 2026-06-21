"""Custom exceptions for the Hunter.io connector."""


class HunterError(Exception):
    """Base exception for all Hunter.io connector errors."""

    def __init__(self, message: str = "", status_code: int = 0, response_body: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class HunterAuthError(HunterError):
    """Raised when the API key is missing, invalid, or revoked (401/403)."""


class HunterNetworkError(HunterError):
    """Raised when the underlying HTTP transport fails (DNS, connect, timeout)."""


class HunterNotFound(HunterError):
    """Raised when the requested resource does not exist (404)."""
