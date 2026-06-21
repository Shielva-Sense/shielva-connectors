"""Telegram connector exception hierarchy.

Mirrors the Wix-style typed exception ladder so the gateway / surfaces can
classify outcomes without parsing strings.
"""
from typing import Any, Dict, Optional


class TelegramError(Exception):
    """Base for all Telegram-connector errors.

    Carries the HTTP status code, the Telegram envelope's ``error_code``
    (which often mirrors but is not strictly equal to the HTTP status), and
    the raw response body for downstream logging.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        error_code: Optional[int] = None,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.response_body = response_body or {}


class TelegramBadRequestError(TelegramError):
    """400 — malformed request body / invalid parameter."""


class TelegramAuthError(TelegramError):
    """401 — bot token invalid, missing, or revoked."""


class TelegramForbiddenError(TelegramError):
    """403 — bot was kicked, blocked, or lacks the required chat permission."""


class TelegramNotFound(TelegramError):
    """404 — chat / message / method does not exist."""


class TelegramConflictError(TelegramError):
    """409 — concurrent getUpdates and webhook configured at the same time."""


class TelegramRateLimitError(TelegramError):
    """429 — rate limited.

    Telegram envelopes for 429 always include ``parameters.retry_after`` (in
    seconds). :attr:`retry_after` exposes that hint so the retry helper can
    sleep precisely instead of guessing.
    """

    def __init__(
        self,
        message: str,
        retry_after: Optional[float] = None,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            message,
            status_code=429,
            error_code=429,
            response_body=response_body,
        )
        self.retry_after = retry_after


class TelegramServerError(TelegramError):
    """5xx — provider-side outage; retry candidate."""


class TelegramNetworkError(TelegramError):
    """Transport-layer failure (DNS, TLS, connection reset, timeout)."""


# Back-compat alias preserved for older test/import paths.
TelegramNotFoundError = TelegramNotFound
