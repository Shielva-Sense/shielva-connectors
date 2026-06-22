from __future__ import annotations


class BasecampError(Exception):
    """Base exception for all Basecamp connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class BasecampAuthError(BasecampError):
    """Raised when Basecamp rejects the access token (401/403)."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message, status_code=status_code, code="auth_error")


class BasecampRateLimitError(BasecampError):
    """Raised on 429 Too Many Requests from Basecamp."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class BasecampNotFoundError(BasecampError):
    """Raised when a requested Basecamp resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class BasecampNetworkError(BasecampError):
    """Raised on transient network failures, timeouts, or 5xx responses."""
