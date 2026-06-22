"""Shortcut connector exception hierarchy."""
from __future__ import annotations

from typing import Any, Dict, Optional


class ShortcutError(Exception):
    """Base for all Shortcut-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class ShortcutAuthError(ShortcutError):
    """401 / 403 — API token invalid, missing, or lacks permissions."""


class ShortcutBadRequestError(ShortcutError):
    """400 / 422 — malformed request body or validation error."""


class ShortcutNotFoundError(ShortcutError):
    """404 — resource not found."""


class ShortcutConflictError(ShortcutError):
    """409 — duplicate / state conflict."""


class ShortcutRateLimitError(ShortcutError):
    """429 — rate limited. `retry_after_s` is the suggested wait."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_body: Optional[Dict[str, Any]] = None,
        retry_after_s: float = 5.0,
    ) -> None:
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class ShortcutServerError(ShortcutError):
    """5xx — provider-side outage; retry candidate."""


class ShortcutNetworkError(ShortcutError):
    """Transport-level failure (DNS, TCP, TLS, timeout)."""


# Back-compat aliases for older code that imports these names.
ShortcutNotFound = ShortcutNotFoundError
ShortcutAPIError = ShortcutError
