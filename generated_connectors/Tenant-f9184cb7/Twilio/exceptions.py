from __future__ import annotations


class TwilioError(Exception):
    """Base exception for all Twilio connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class TwilioAuthError(TwilioError):
    """Raised when Twilio rejects credentials (401/403)."""


class TwilioRateLimitError(TwilioError):
    """Raised on 429 Too Many Requests from Twilio."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class TwilioNotFoundError(TwilioError):
    """Raised when a requested Twilio resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(f"{resource} '{resource_id}' not found", status_code=404, code="resource_missing")


class TwilioNetworkError(TwilioError):
    """Raised on transient network failures (timeouts, connection errors)."""
