"""Custom exceptions for the Google Drive connector."""
from __future__ import annotations


class GoogleDriveError(Exception):
    """Base exception for all Google Drive connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class GoogleDriveAuthError(GoogleDriveError):
    """Raised when Google Drive rejects the token (401)."""


class GoogleDriveNetworkError(GoogleDriveError):
    """Raised on transient network failures (timeouts, connection errors)."""


class GoogleDriveNotFoundError(GoogleDriveError):
    """Raised when a requested Google Drive resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class GoogleDriveRateLimitError(GoogleDriveError):
    """Raised on 429 Too Many Requests from Google Drive API."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after
