from __future__ import annotations


class DropboxError(Exception):
    """Base exception for all Dropbox connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class DropboxAuthError(DropboxError):
    """Raised when Dropbox rejects the token (401/403)."""


class DropboxRateLimitError(DropboxError):
    """Raised on 429 Too Many Requests from Dropbox."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class DropboxNotFoundError(DropboxError):
    """Raised when a requested Dropbox resource does not exist (409/not_found path)."""

    def __init__(self, resource: str, path: str) -> None:
        super().__init__(
            f"{resource} '{path}' not found",
            status_code=409,
            code="not_found",
        )


class DropboxNetworkError(DropboxError):
    """Raised on transient network failures (timeouts, connection errors)."""
