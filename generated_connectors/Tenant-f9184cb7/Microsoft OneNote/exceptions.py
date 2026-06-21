"""Custom exceptions for the Microsoft OneNote connector."""


class OneNoteError(Exception):
    """Base exception for all OneNote connector errors."""

    def __init__(self, message: str = "", status_code: int = 0, response_body: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class OneNoteAuthError(OneNoteError):
    """Raised when Microsoft Graph authentication fails or token is invalid (401)."""


class OneNoteNetworkError(OneNoteError):
    """Raised when a network-level failure occurs (timeouts, connection errors, 5xx)."""


class OneNoteNotFound(OneNoteError):
    """Raised when a notebook / section / page is not found (404)."""


class OneNoteRateLimitError(OneNoteError):
    """Raised when Microsoft Graph throttles the request (429).

    Carries the server-provided ``Retry-After`` (in seconds) so callers can
    respect the throttling window. This is a subclass of OneNoteError so
    callers that catch the base class still see it; specialised callers can
    catch this directly to read ``retry_after``.
    """

    def __init__(self, message: str = "", retry_after: float = 0.0, response_body: dict = None):
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after = retry_after
