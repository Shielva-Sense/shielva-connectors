"""Hubstaff connector exception hierarchy."""
from typing import Any, Dict, Optional


class HubstaffError(Exception):
    """Base for all Hubstaff-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class HubstaffAuthError(HubstaffError):
    """401 / 403 — access token invalid, missing, or lacks permissions."""


class HubstaffBadRequestError(HubstaffError):
    """400 — malformed request body / query parameters."""


class HubstaffNotFoundError(HubstaffError):
    """404 — resource not found."""


class HubstaffConflictError(HubstaffError):
    """409 — duplicate / state conflict."""


class HubstaffRateLimitError(HubstaffError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class HubstaffServerError(HubstaffError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
HubstaffNetworkError = HubstaffServerError
HubstaffNotFound = HubstaffNotFoundError
