from __future__ import annotations


class WorkableError(Exception):
    """Base exception for all Workable connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class WorkableAuthError(WorkableError):
    """Raised when Workable rejects the API token (401/403)."""


class WorkableNetworkError(WorkableError):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""


class WorkableRateLimitError(WorkableError):
    """Raised on 429 Too Many Requests from the Workable API."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class WorkableNotFoundError(WorkableError):
    """Raised when a requested resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="not_found",
        )
