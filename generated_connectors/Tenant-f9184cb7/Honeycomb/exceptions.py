"""Honeycomb connector exception hierarchy.

Every error raised out of `client/http_client.py` is a `HoneycombError`
subclass that carries `status_code` and `response_body` so callers can
classify without re-parsing httpx responses.
"""
from typing import Any, Dict, Optional


class HoneycombError(Exception):
    """Base for all Honeycomb connector errors."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class HoneycombAuthError(HoneycombError):
    """401 / 403 — API key missing, invalid, or lacks required scope."""


class HoneycombBadRequestError(HoneycombError):
    """400 — malformed request body / invalid params."""


class HoneycombNotFoundError(HoneycombError):
    """404 — resource (dataset / query / trigger / board / SLO / marker) not found."""


class HoneycombConflictError(HoneycombError):
    """409 — duplicate / state conflict."""


class HoneycombRateLimitError(HoneycombError):
    """429 — Honeycomb per-environment rate limit exceeded.

    `retry_after_s` mirrors the `Retry-After` response header when Honeycomb
    surfaces it (it normally does).
    """

    def __init__(self, message: str, status_code: int = 429,
                 response_body: Optional[Dict[str, Any]] = None,
                 retry_after_s: float = 5.0):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class HoneycombServerError(HoneycombError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases used by older callers / tests.
HoneycombNetworkError = HoneycombServerError
HoneycombNotFound = HoneycombNotFoundError
