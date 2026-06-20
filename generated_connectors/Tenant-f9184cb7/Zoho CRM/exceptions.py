from __future__ import annotations


class ZohoCRMError(Exception):
    """Base exception for all Zoho CRM connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class ZohoCRMAuthError(ZohoCRMError):
    """Raised when Zoho rejects the access token (401/403)."""


class ZohoCRMRateLimitError(ZohoCRMError):
    """Raised on 429 Too Many Requests from Zoho."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class ZohoCRMNotFoundError(ZohoCRMError):
    """Raised when a requested Zoho resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="NOT_FOUND",
        )


class ZohoCRMNetworkError(ZohoCRMError):
    """Raised on transient network failures (timeouts, connection errors)."""


class ZohoCRMServerError(ZohoCRMError):
    """Raised on 5xx responses from Zoho."""
