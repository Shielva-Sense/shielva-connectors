"""Custom exceptions for the Make connector."""


class MakeError(Exception):
    """Base exception for all Make connector errors."""

    def __init__(self, message: str = "", status_code: int = 0, response_body: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class MakeAuthError(MakeError):
    """Raised when authentication fails or the API token is invalid (401/403)."""


class MakeNetworkError(MakeError):
    """Raised on transport-level failures (DNS, TCP, TLS, timeouts, 5xx)."""


class MakeNotFound(MakeError):
    """Raised when the requested Make resource is not found (404)."""


class MakeRateLimitError(MakeError):
    """Raised when the Make API rate limit is exceeded (429)."""
