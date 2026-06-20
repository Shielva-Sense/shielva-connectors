"""Loom connector — custom exception hierarchy."""
from __future__ import annotations


class LoomError(Exception):
    """Base exception for all Loom connector errors."""


class LoomAuthError(LoomError):
    """Raised on authentication failures — invalid API key, unauthorized (401/403)."""


class LoomNetworkError(LoomError):
    """Raised on connection / timeout failures."""


class LoomNotFoundError(LoomError):
    """Raised when a requested resource does not exist (404)."""


class LoomRateLimitError(LoomError):
    """Raised when Loom API returns 429 Too Many Requests."""
