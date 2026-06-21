"""Custom exceptions for the Clockify connector."""


class ClockifyError(Exception):
    """Base exception for all Clockify connector errors."""

    def __init__(self, message: str = "", status_code: int = 0, response_body: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class ClockifyAuthError(ClockifyError):
    """Raised when authentication fails or the API key is invalid (401/403)."""


class ClockifyNetworkError(ClockifyError):
    """Raised when a network-level failure prevents the request from completing."""


class ClockifyNotFound(ClockifyError):
    """Raised when a resource (workspace, project, time entry, etc.) is not found (404)."""


class ClockifyRateLimitError(ClockifyError):
    """Raised when the Clockify API rate limit is exceeded (429)."""
