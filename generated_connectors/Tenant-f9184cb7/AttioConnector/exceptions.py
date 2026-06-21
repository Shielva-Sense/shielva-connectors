"""Attio connector exception hierarchy."""
from typing import Any, Dict, Optional


class AttioError(Exception):
    """Base for all Attio-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class AttioAuthError(AttioError):
    """401 / 403 — access token invalid, missing, or lacks scopes."""


class AttioBadRequestError(AttioError):
    """400 — malformed request body."""


class AttioNotFoundError(AttioError):
    """404 — resource not found."""


class AttioConflictError(AttioError):
    """409 — duplicate / state conflict."""


class AttioRateLimitError(AttioError):
    """429 — rate limited. ``retry_after_s`` is the suggested wait."""

    def __init__(
        self,
        message: str,
        retry_after_s: float = 5.0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after_s = retry_after_s


class AttioServerError(AttioError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
AttioConnectorError = AttioError
AttioAPIError = AttioError
AttioNetworkError = AttioServerError
AttioNotFound = AttioNotFoundError
