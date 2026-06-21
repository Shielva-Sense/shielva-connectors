"""Toggl Track connector exception hierarchy."""
from __future__ import annotations

from typing import Any, Dict, Optional


class TogglError(Exception):
    """Base for all Toggl Track connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class TogglAuthError(TogglError):
    """401 / 403 — API token invalid, password literal wrong, or lacks permissions."""


class TogglBadRequestError(TogglError):
    """400 — malformed request body."""


class TogglNotFoundError(TogglError):
    """404 — resource not found."""


class TogglConflictError(TogglError):
    """409 — duplicate / state conflict."""


class TogglRateLimitError(TogglError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class TogglServerError(TogglError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases (older imports use these names).
TogglNetworkError = TogglServerError
TogglNotFound = TogglNotFoundError
