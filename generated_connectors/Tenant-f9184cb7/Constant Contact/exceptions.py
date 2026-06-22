"""Constant Contact connector exception hierarchy.

All exceptions inherit from ConstantContactError so callers can catch
the base class when they do not care about the specific error type.
"""
from __future__ import annotations


class ConstantContactError(Exception):
    """Base exception for all Constant Contact connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class ConstantContactAuthError(ConstantContactError):
    """Raised when Constant Contact rejects the access token (401/403)."""


class ConstantContactNetworkError(ConstantContactError):
    """Raised on transient network failures (timeouts, connection errors)."""


class ConstantContactNotFoundError(ConstantContactError):
    """Raised when a requested Constant Contact resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class ConstantContactRateLimitError(ConstantContactError):
    """Raised on 429 Too Many Requests from Constant Contact."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after
