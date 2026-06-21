"""Custom exceptions for the Plivo connector."""


class PlivoError(Exception):
    """Base exception for all Plivo connector errors."""

    def __init__(self, message: str = "", status_code: int = 0, response_body: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class PlivoAuthError(PlivoError):
    """Raised when Plivo authentication fails (401 / bad auth_id or auth_token)."""


class PlivoNetworkError(PlivoError):
    """Raised when a network-level failure prevents reaching the Plivo API."""


class PlivoNotFound(PlivoError):
    """Raised when a Plivo resource (call, message, number, application) is not found (404)."""


class PlivoRateLimitError(PlivoError):
    """Raised when the Plivo API rate limit is exceeded (429)."""
