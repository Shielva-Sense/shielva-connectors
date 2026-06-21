"""Custom exceptions raised by the Keap connector and its HTTP client."""
from typing import Any, Dict, Optional


class KeapError(Exception):
    """Base exception for all Keap connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body or {}


class KeapAuthError(KeapError):
    """Raised on 401/403 — the access token is invalid, revoked, or expired."""


class KeapNotFound(KeapError):
    """Raised on 404 — the requested Keap resource does not exist."""

    def __init__(self, resource: str, resource_id: str = "") -> None:
        super().__init__(
            f"Keap {resource} '{resource_id}' not found",
            status_code=404,
        )
        self.resource = resource
        self.resource_id = resource_id


class KeapRateLimitError(KeapError):
    """Raised on 429 — the Keap API rate limit has been exceeded.

    Carries the optional ``retry_after`` value (seconds) parsed from the
    ``Retry-After`` response header so callers can wait the provider-recommended
    duration before retrying.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class KeapNetworkError(KeapError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""
