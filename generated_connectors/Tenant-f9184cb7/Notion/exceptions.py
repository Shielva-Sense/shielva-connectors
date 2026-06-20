"""Notion connector — custom exception hierarchy."""
from __future__ import annotations


class NotionError(Exception):
    """Base exception for all Notion connector errors."""


class NotionAuthError(NotionError):
    """Raised on authentication failures — invalid token, unauthorized."""


class NotionNetworkError(NotionError):
    """Raised on connection / timeout failures."""


class NotionRateLimitError(NotionError):
    """Raised when Notion API returns 429 Too Many Requests."""


class NotionNotFoundError(NotionError):
    """Raised when a requested resource does not exist (404)."""
