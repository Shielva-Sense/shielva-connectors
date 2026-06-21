"""ADP connector exception hierarchy."""


class ADPError(Exception):
    """Base for all ADP-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class ADPAuthError(ADPError):
    """401 / 403 — OAuth2 token mint failed, token expired, or app not entitled."""


class ADPBadRequestError(ADPError):
    """400 — malformed request body or invalid OData filter."""


class ADPNotFoundError(ADPError):
    """404 — resource not found."""


class ADPConflictError(ADPError):
    """409 — duplicate / state conflict (e.g. duplicate time-off request)."""


class ADPRateLimitError(ADPError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class ADPServerError(ADPError):
    """5xx — provider-side outage; retry candidate."""


class ADPAPIError(ADPError):
    """Any other unexpected 4xx response not classified above."""


# Back-compat aliases for older code that imports these names.
ADPNetworkError = ADPServerError
ADPNotFound = ADPNotFoundError
