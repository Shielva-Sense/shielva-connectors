"""Plausible Analytics connector exception hierarchy."""
from __future__ import annotations

from typing import Any, Dict, Optional


class PlausibleError(Exception):
    """Base for all Plausible-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class PlausibleAuthError(PlausibleError):
    """401 / 403 — API key invalid, missing, or lacks scope."""


class PlausibleBadRequestError(PlausibleError):
    """400 — malformed request body / query string."""


class PlausibleNotFound(PlausibleError):
    """404 — resource not found (site, goal, …)."""


class PlausibleRateLimitError(PlausibleError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class PlausibleNetworkError(PlausibleError):
    """Transport-level error or 5xx server error — retry candidate."""


class PlausibleAPIError(PlausibleError):
    """Unexpected non-2xx response (4xx other than 401/403/404/429)."""


# Back-compat alias used by older importers.
PlausibleNotFoundError = PlausibleNotFound
PlausibleServerError = PlausibleNetworkError
