from __future__ import annotations


class TableauError(Exception):
    """Base exception for all Tableau connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class TableauAuthError(TableauError):
    """Raised when Tableau rejects the Personal Access Token (401/403)."""


class TableauRateLimitError(TableauError):
    """Raised on 429 Too Many Requests from the Tableau REST API."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class TableauNotFoundError(TableauError):
    """Raised when a requested Tableau resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="NOT_FOUND",
        )


class TableauNetworkError(TableauError):
    """Raised on transient network failures (timeouts, connection errors)."""
