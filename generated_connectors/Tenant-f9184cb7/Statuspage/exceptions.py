"""Statuspage connector exception hierarchy."""
from __future__ import annotations

from typing import Optional


class StatuspageError(Exception):
    """Base for all Statuspage-connector errors.

    Carries the HTTP status code (when known) and the raw response body so
    higher layers can render a useful diagnostic without re-parsing.
    """

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: Optional[dict] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class StatuspageAuthError(StatuspageError):
    """401 / 403 — API token rejected or lacks scope."""


class StatuspageBadRequestError(StatuspageError):
    """400 — malformed request body or invalid query."""


class StatuspageNotFoundError(StatuspageError):
    """404 — page / component / incident / subscriber / metric not found."""


class StatuspageConflictError(StatuspageError):
    """409 — duplicate / state conflict (e.g. duplicate component name)."""


class StatuspageRateLimitError(StatuspageError):
    """429 — rate limited. ``retry_after_s`` carries the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class StatuspageServerError(StatuspageError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases — older call sites still import these names.
StatuspageNetworkError = StatuspageServerError
StatuspageNotFound = StatuspageNotFoundError
