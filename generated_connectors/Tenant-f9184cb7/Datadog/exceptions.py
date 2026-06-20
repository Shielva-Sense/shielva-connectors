from __future__ import annotations


class DatadogError(Exception):
    """Base exception for all Datadog connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class DatadogAuthError(DatadogError):
    """Raised when Datadog rejects the API key or Application key (401/403)."""


class DatadogNetworkError(DatadogError):
    """Raised on transient network failures (timeouts, connection errors)."""


class DatadogNotFoundError(DatadogError):
    """Raised when a requested Datadog resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class DatadogRateLimitError(DatadogError):
    """Raised on 429 Too Many Requests from Datadog."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after
