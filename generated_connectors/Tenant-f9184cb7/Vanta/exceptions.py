"""Vanta connector exception hierarchy."""
from __future__ import annotations

from typing import Any, Dict, Optional


class VantaError(Exception):
    """Base for all Vanta-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class VantaAuthError(VantaError):
    """401 / 403 — OAuth token invalid, expired, or scope insufficient."""


class VantaBadRequestError(VantaError):
    """400 — malformed request body."""


class VantaNotFoundError(VantaError):
    """404 — resource not found."""


class VantaConflictError(VantaError):
    """409 — duplicate / state conflict."""


class VantaRateLimitError(VantaError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_body: Optional[Dict[str, Any]] = None,
        retry_after_s: float = 5.0,
    ):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class VantaServerError(VantaError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
VantaNetworkError = VantaServerError
VantaNotFound = VantaNotFoundError
