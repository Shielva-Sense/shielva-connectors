"""Monday.com connector — custom exception hierarchy."""
from __future__ import annotations


class MondayError(Exception):
    """Base exception for all Monday.com connector errors."""


class MondayAuthError(MondayError):
    """Raised on authentication failures — invalid or expired API token."""


class MondayNetworkError(MondayError):
    """Raised on connection / timeout failures."""


class MondayRateLimitError(MondayError):
    """Raised when Monday.com API returns a rate-limit error."""


class MondayNotFoundError(MondayError):
    """Raised when a requested resource does not exist (board not found, item not found)."""
