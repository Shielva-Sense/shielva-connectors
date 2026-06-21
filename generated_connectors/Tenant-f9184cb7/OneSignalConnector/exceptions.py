"""OneSignal connector exception hierarchy."""
from __future__ import annotations

from typing import Any, Dict, Optional


class OneSignalError(Exception):
    """Base for all OneSignal-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class OneSignalAuthError(OneSignalError):
    """401 / 403 — REST API key / User Auth Key invalid, missing, or lacks permissions.

    NOTE: 401 from OneSignal usually means either (a) the key is wrong, or
    (b) the caller forgot the literal ``Basic `` prefix in the Authorization
    header. OneSignal expects ``Authorization: Basic <raw_key>`` — the key is
    NOT base64-encoded.
    """


class OneSignalBadRequestError(OneSignalError):
    """400 — malformed request body (e.g. missing ``app_id`` or empty audience)."""


class OneSignalNotFoundError(OneSignalError):
    """404 — app / notification / player / segment not found."""


class OneSignalConflictError(OneSignalError):
    """409 — duplicate / state conflict (e.g. segment name already taken)."""


class OneSignalRateLimitError(OneSignalError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class OneSignalServerError(OneSignalError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older callers that imported these names.
OneSignalNetworkError = OneSignalServerError
OneSignalNotFound = OneSignalNotFoundError
