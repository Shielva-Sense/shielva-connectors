"""JazzHR connector exception hierarchy.

All exceptions raised by `client/http_client.py` and re-thrown to callers
extend `JazzHRError`. Each carries the upstream HTTP `status_code` and the
parsed `response_body` (best-effort) so the gateway can log / classify.
"""
from typing import Any, Dict, Optional


class JazzHRError(Exception):
    """Base exception for all JazzHR connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class JazzHRAuthError(JazzHRError):
    """401 / 403 — API key invalid, missing, or revoked."""


class JazzHRBadRequestError(JazzHRError):
    """400 — malformed request body (typically a missing required form field)."""


class JazzHRNotFound(JazzHRError):
    """404 — resource not found."""


class JazzHRRateLimitError(JazzHRError):
    """429 — rate limited. `retry_after_s` is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class JazzHRServerError(JazzHRError):
    """5xx — provider-side outage."""


# Network errors (transport failures, retry-exhausted 5xx / 429).
# Kept as a separate alias so callers can write `except JazzHRNetworkError`
# without caring whether it originated as a transport failure or as a
# retry-exhausted upstream error.
JazzHRNetworkError = JazzHRServerError


# Back-compat alias: older normalizers / tests may import `JazzHRNotFoundError`.
JazzHRNotFoundError = JazzHRNotFound
