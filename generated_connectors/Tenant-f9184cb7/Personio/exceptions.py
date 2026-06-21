"""Personio connector exception hierarchy."""
from __future__ import annotations

from typing import Optional


class PersonioError(Exception):
    """Base for all Personio-connector errors.

    Carries the upstream HTTP status code + parsed body so health_check and
    error reporters can classify without re-parsing.
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


class PersonioAuthError(PersonioError):
    """401 / 403 — token invalid, missing, expired, or lacks scope."""


class PersonioBadRequestError(PersonioError):
    """400 — validation failure, missing required field."""


class PersonioNotFoundError(PersonioError):
    """404 — resource not found."""


class PersonioConflictError(PersonioError):
    """409 — duplicate / state conflict (e.g. employee email already exists)."""


class PersonioRateLimitError(PersonioError):
    """429 — rate limited. retry_after_s carries the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class PersonioServerError(PersonioError):
    """5xx — provider outage; retry candidate."""


class PersonioNetworkError(PersonioError):
    """Transport-level failure (timeout, DNS, connection reset)."""


# Back-compat aliases for older imports.
PersonioNotFound = PersonioNotFoundError
