"""Crisp connector exception hierarchy."""
from typing import Optional


class CrispError(Exception):
    """Base for all Crisp-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[dict] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class CrispAuthError(CrispError):
    """401 / 403 — plugin credentials invalid, missing, or lack scope."""


class CrispBadRequestError(CrispError):
    """400 — malformed request body."""


class CrispNotFoundError(CrispError):
    """404 — resource not found."""


class CrispConflictError(CrispError):
    """409 — duplicate / state conflict (e.g. existing contact email)."""


class CrispRateLimitError(CrispError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_body: Optional[dict] = None,
        retry_after_s: float = 5.0,
    ):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class CrispServerError(CrispError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
CrispNetworkError = CrispServerError
CrispNotFound = CrispNotFoundError
