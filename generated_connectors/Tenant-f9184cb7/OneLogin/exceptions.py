"""OneLogin connector exception hierarchy.

All exceptions inherit from ``OneLoginError`` and carry an HTTP ``status_code``
and the parsed ``response_body``. The connector layer catches the typed
subclasses (Auth / NotFound / RateLimit / Network) and maps them through
``_STATUS_MAP`` to ``(ConnectorHealth, AuthStatus)`` pairs.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class OneLoginError(Exception):
    """Base exception for all OneLogin connector errors."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class OneLoginAuthError(OneLoginError):
    """401 / 403 — token invalid, expired, or lacks scope."""


class OneLoginBadRequestError(OneLoginError):
    """400 — malformed request body."""


class OneLoginNotFoundError(OneLoginError):
    """404 — resource not found."""


class OneLoginConflictError(OneLoginError):
    """409 — duplicate / state conflict."""


class OneLoginRateLimitError(OneLoginError):
    """429 — rate limited. ``retry_after_s`` is the server-suggested wait."""

    def __init__(
        self,
        message: str,
        retry_after_s: float = 1.0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after_s = retry_after_s


class OneLoginServerError(OneLoginError):
    """5xx — provider-side outage; transport-level error candidate."""


# Back-compat aliases for older code (kept stable across refactors).
OneLoginNetworkError = OneLoginServerError
OneLoginNotFound = OneLoginNotFoundError
