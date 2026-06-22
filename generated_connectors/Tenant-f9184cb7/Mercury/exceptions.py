"""Mercury connector exception hierarchy."""
from __future__ import annotations

from typing import Any, Dict, Optional


class MercuryError(Exception):
    """Base for all Mercury-connector errors."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class MercuryAuthError(MercuryError):
    """401 / 403 — token invalid, missing, or lacks scope."""


class MercuryBadRequestError(MercuryError):
    """400 — malformed request body / query."""


class MercuryNotFoundError(MercuryError):
    """404 — resource not found."""


class MercuryConflictError(MercuryError):
    """409 — duplicate / state conflict."""


class MercuryRateLimitError(MercuryError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class MercuryServerError(MercuryError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
MercuryNetworkError = MercuryServerError
MercuryNotFound = MercuryNotFoundError
