"""Custom exceptions for the Outlook Mail connector."""


class OutlookMailError(Exception):
    """Base exception for all Outlook Mail connector errors."""

    def __init__(self, message: str = "", status_code: int = 0, response_body: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class OutlookMailAuthError(OutlookMailError):
    """Raised when Microsoft Graph authentication fails or token is invalid (401)."""


class OutlookMailNetworkError(OutlookMailError):
    """Raised when a network-level failure occurs (timeouts, connection errors, 5xx)."""


class OutlookMailNotFound(OutlookMailError):
    """Raised when a Microsoft Graph resource is not found (404)."""


class OutlookMailRateLimitError(OutlookMailError):
    """Raised when Microsoft Graph throttles the request (429).

    Carries the server-provided ``Retry-After`` (in seconds) so callers can
    respect the throttling window.  This is a subclass of OutlookMailError so
    callers that catch the base class still see it; specialised callers can
    catch this directly to read ``retry_after``.
    """

    def __init__(self, message: str = "", retry_after: float = 0.0, response_body: dict = None):
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after = retry_after
