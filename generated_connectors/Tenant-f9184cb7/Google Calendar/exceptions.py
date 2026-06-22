"""Custom exceptions for the Google Calendar connector."""


class GoogleCalendarError(Exception):
    """Base exception for all Google Calendar connector errors."""


class GoogleCalendarAuthError(GoogleCalendarError):
    """Raised when authentication fails or a token is invalid/expired."""


class GoogleCalendarRateLimitError(GoogleCalendarError):
    """Raised when the Google Calendar API rate limit (429) is exceeded."""


class GoogleCalendarNetworkError(GoogleCalendarError):
    """Raised when a network-level error prevents the API call from completing."""


class GoogleCalendarNotFoundError(GoogleCalendarError):
    """Raised when a requested resource (calendar or event) is not found (404)."""
