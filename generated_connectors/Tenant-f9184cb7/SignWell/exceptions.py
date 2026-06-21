"""SignWell connector exception hierarchy.

Every typed exception carries the originating HTTP status_code and the parsed
response_body dict so the gateway / health_check classifier can route on them.
"""
from typing import Optional


class SignWellError(Exception):
    """Base for all SignWell-connector errors."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: Optional[dict] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class SignWellAuthError(SignWellError):
    """401 / 403 — API key invalid, missing, or lacks permissions."""


class SignWellBadRequestError(SignWellError):
    """400 — malformed request body / validation error."""


class SignWellNotFoundError(SignWellError):
    """404 — resource not found (document, template, recipient, webhook)."""


class SignWellConflictError(SignWellError):
    """409 — duplicate / state conflict (e.g. cancelling a completed document)."""


class SignWellRateLimitError(SignWellError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 429,
        response_body: Optional[dict] = None,
        retry_after_s: float = 1.0,
    ):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class SignWellServerError(SignWellError):
    """5xx — provider-side outage; retry candidate."""


class SignWellNetworkError(SignWellError):
    """Transport-level failure (timeout / DNS / reset) — retries exhausted."""


# Back-compat aliases for older code that imports these names.
SignWellNotFound = SignWellNotFoundError
