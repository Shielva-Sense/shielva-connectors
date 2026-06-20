from __future__ import annotations


class GoogleSheetsError(Exception):
    """Base exception for all Google Sheets connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class GoogleSheetsAuthError(GoogleSheetsError):
    """Raised when Google rejects the OAuth token (401/403)."""


class GoogleSheetsNetworkError(GoogleSheetsError):
    """Raised on transient network failures (timeouts, connection errors)."""


class GoogleSheetsRateLimitError(GoogleSheetsError):
    """Raised on 429 Too Many Requests from the Google APIs."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class GoogleSheetsNotFoundError(GoogleSheetsError):
    """Raised when a requested spreadsheet or range does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="not_found",
        )
