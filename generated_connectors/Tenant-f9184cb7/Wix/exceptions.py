"""Wix connector exception hierarchy."""


class WixError(Exception):
    """Base for all Wix-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class WixAuthError(WixError):
    """401 / 403 — API key invalid, missing, or lacks permissions."""


class WixBadRequestError(WixError):
    """400 — malformed request body."""


class WixNotFoundError(WixError):
    """404 — resource not found."""


class WixConflictError(WixError):
    """409 — duplicate / state conflict."""


class WixPreconditionError(WixError):
    """428 — precondition required (revision mismatch)."""


class WixRateLimitError(WixError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class WixServerError(WixError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
WixNetworkError = WixServerError
WixNotFound = WixNotFoundError
