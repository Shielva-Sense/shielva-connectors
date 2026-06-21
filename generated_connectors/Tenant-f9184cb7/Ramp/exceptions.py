"""Ramp connector exception hierarchy."""


class RampError(Exception):
    """Base for all Ramp-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class RampAuthError(RampError):
    """401 / 403 — OAuth2 client credentials invalid, expired, or lack scopes."""


class RampBadRequestError(RampError):
    """400 — malformed request body."""


class RampNotFoundError(RampError):
    """404 — resource not found."""


class RampConflictError(RampError):
    """409 — duplicate / state conflict."""


class RampRateLimitError(RampError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class RampServerError(RampError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
RampNetworkError = RampServerError
RampNotFound = RampNotFoundError
