from __future__ import annotations


class SalesforceError(Exception):
    """Base exception for all Salesforce connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class SalesforceAuthError(SalesforceError):
    """Raised when Salesforce rejects the access token (401/403)."""


class SalesforceRateLimitError(SalesforceError):
    """Raised on 429 Too Many Requests from Salesforce."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class SalesforceNotFoundError(SalesforceError):
    """Raised when a requested Salesforce resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="NOT_FOUND",
        )


class SalesforceNetworkError(SalesforceError):
    """Raised on transient network failures (timeouts, connection errors)."""


class SalesforceServerError(SalesforceError):
    """Raised on 5xx responses from Salesforce."""
