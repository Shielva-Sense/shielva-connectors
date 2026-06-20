from __future__ import annotations


class ZendeskSellError(Exception):
    """Base exception for all Zendesk Sell connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class ZendeskSellAuthError(ZendeskSellError):
    """Raised when Zendesk Sell rejects the access token (401/403)."""


class ZendeskSellRateLimitError(ZendeskSellError):
    """Raised on 429 Too Many Requests from Zendesk Sell."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class ZendeskSellNotFoundError(ZendeskSellError):
    """Raised when a requested Zendesk Sell resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class ZendeskSellNetworkError(ZendeskSellError):
    """Raised on transient network failures (timeouts, connection errors)."""


class ZendeskSellServerError(ZendeskSellError):
    """Raised on 5xx responses from Zendesk Sell."""
