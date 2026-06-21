"""Brex connector exception hierarchy."""


class BrexError(Exception):
    """Base for all Brex-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class BrexAuthError(BrexError):
    """401 / 403 — access token invalid, missing, or lacks permissions."""


class BrexBadRequestError(BrexError):
    """400 — malformed request body or query params."""


class BrexNotFoundError(BrexError):
    """404 — resource not found."""


class BrexConflictError(BrexError):
    """409 — duplicate / state conflict."""


class BrexRateLimitError(BrexError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class BrexServerError(BrexError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older imports.
BrexNetworkError = BrexServerError
BrexNotFound = BrexNotFoundError
