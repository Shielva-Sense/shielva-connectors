"""Loggly connector exception hierarchy."""


class LogglyError(Exception):
    """Base for all Loggly-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class LogglyAuthError(LogglyError):
    """401 / 403 — Basic-auth credentials invalid or token lacks permission."""


class LogglyBadRequestError(LogglyError):
    """400 — malformed search query or request body."""


class LogglyNotFoundError(LogglyError):
    """404 — subdomain wrong or resource missing."""


class LogglyConflictError(LogglyError):
    """409 — duplicate / state conflict."""


class LogglyRateLimitError(LogglyError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class LogglyServerError(LogglyError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases preserved for older imports.
LogglyNetworkError = LogglyServerError
LogglyNotFound = LogglyNotFoundError
