from __future__ import annotations


class WhatsAppError(Exception):
    """Base exception for all WhatsApp Business connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class WhatsAppAuthError(WhatsAppError):
    """Raised when Meta rejects the access token (error code 190)."""


class WhatsAppNetworkError(WhatsAppError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""


class WhatsAppRateLimitError(WhatsAppError):
    """Raised on 429 Too Many Requests from the Meta Graph API."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code=429)
        self.retry_after = retry_after


class WhatsAppNotFoundError(WhatsAppError):
    """Raised when the requested resource does not exist (error code 100 + subcode 33)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(f"{resource} '{resource_id}' not found", status_code=400, code=100)
        self.resource = resource
        self.resource_id = resource_id
