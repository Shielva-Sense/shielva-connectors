"""Monday.com connector — custom exception hierarchy."""
from __future__ import annotations


class MondayComError(Exception):
    """Base exception for all Monday.com connector errors."""


class MondayComAuthError(MondayComError):
    """Raised on authentication failures — invalid or expired API token."""


class MondayComNetworkError(MondayComError):
    """Raised on connection / timeout / server errors."""


class MondayComNotFoundError(MondayComError):
    """Raised when a requested resource does not exist."""


class MondayComRateLimitError(MondayComError):
    """Raised when Monday.com API returns a rate-limit (HTTP 429) error."""
