"""Lightspeed Retail connector exception hierarchy."""
from typing import Optional


class LightspeedError(Exception):
    """Base for all Lightspeed-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[dict] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class LightspeedAuthError(LightspeedError):
    """401 / 403 — token invalid, expired, or scope insufficient."""


class LightspeedBadRequestError(LightspeedError):
    """400 — malformed request body or query."""


class LightspeedNotFound(LightspeedError):
    """404 — resource not found."""


class LightspeedConflictError(LightspeedError):
    """409 — duplicate / state conflict."""


class LightspeedRateLimitError(LightspeedError):
    """429 — leaky-bucket overflow. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 1.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class LightspeedServerError(LightspeedError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
LightspeedNetworkError = LightspeedServerError
LightspeedNotFoundError = LightspeedNotFound
