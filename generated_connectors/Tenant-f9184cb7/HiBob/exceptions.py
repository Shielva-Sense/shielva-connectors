"""HiBob connector exception hierarchy."""
from __future__ import annotations

from typing import Any, Dict, Optional


class HiBobError(Exception):
    """Base for all HiBob-connector errors."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class HiBobAuthError(HiBobError):
    """401 / 403 — service-user invalid, disabled, or lacks role."""


class HiBobBadRequestError(HiBobError):
    """400 — malformed request body."""


class HiBobNotFoundError(HiBobError):
    """404 — resource not found."""


class HiBobConflictError(HiBobError):
    """409 — duplicate / state conflict."""


class HiBobRateLimitError(HiBobError):
    """429 — rate limited. ``retry_after_s`` is the suggested wait."""

    def __init__(self, message: str = "", retry_after_s: float = 5.0) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class HiBobServerError(HiBobError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
HiBobNetworkError = HiBobServerError
HiBobNotFound = HiBobNotFoundError
