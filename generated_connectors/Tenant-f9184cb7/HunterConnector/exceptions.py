"""Hunter.io connector exception hierarchy."""
from __future__ import annotations

from typing import Any, Dict, Optional


class HunterError(Exception):
    """Base exception for all Hunter.io connector errors."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class HunterAuthError(HunterError):
    """401 / 403 — API key missing, invalid, revoked, or over plan quota."""


class HunterNotFoundError(HunterError):
    """404 — resource not found."""


class HunterRateLimitError(HunterError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 1.0) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class HunterServerError(HunterError):
    """5xx — provider-side outage; retry candidate."""


class HunterNetworkError(HunterError):
    """Underlying HTTP transport failure (DNS, connect, timeout)."""


# Back-compat aliases for older code that imports these names.
HunterNotFound = HunterNotFoundError
