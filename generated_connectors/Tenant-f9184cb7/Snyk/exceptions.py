"""Snyk connector exception hierarchy."""
from typing import Optional


class SnykError(Exception):
    """Base for all Snyk-connector errors."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: Optional[dict] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body or {}


class SnykAuthError(SnykError):
    """401 / 403 — API token invalid, missing, or lacks permissions."""


class SnykBadRequestError(SnykError):
    """400 — malformed request body or query."""


class SnykNotFoundError(SnykError):
    """404 — resource not found."""


class SnykConflictError(SnykError):
    """409 — duplicate / state conflict."""


class SnykRateLimitError(SnykError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_body: Optional[dict] = None,
        retry_after_s: float = 1.0,
    ):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class SnykServerError(SnykError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older callers.
SnykNetworkError = SnykServerError
SnykNotFound = SnykNotFoundError
