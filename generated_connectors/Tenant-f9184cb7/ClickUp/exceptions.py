"""ClickUp connector — custom exception hierarchy."""
from __future__ import annotations


class ClickUpError(Exception):
    """Base exception for all ClickUp connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class ClickUpAuthError(ClickUpError):
    """Raised on authentication failures (401/403) — invalid API key."""


class ClickUpNetworkError(ClickUpError):
    """Raised on connection / timeout failures or 5xx responses."""


class ClickUpNotFoundError(ClickUpError):
    """Raised when a requested resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str = "") -> None:
        msg = f"{resource} '{resource_id}' not found" if resource_id else f"{resource} not found"
        super().__init__(msg, status_code=404, code="resource_missing")


class ClickUpRateLimitError(ClickUpError):
    """Raised when ClickUp API returns 429 Too Many Requests."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after
