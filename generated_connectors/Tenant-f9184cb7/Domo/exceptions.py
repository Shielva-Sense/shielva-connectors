"""Domo connector — custom exception hierarchy."""
from __future__ import annotations


class DomoError(Exception):
    """Base exception for all Domo connector errors."""


class DomoAuthError(DomoError):
    """Raised on authentication failures — invalid credentials, unauthorized (401/403)."""


class DomoNetworkError(DomoError):
    """Raised on connection / timeout failures."""


class DomoNotFoundError(DomoError):
    """Raised when a requested resource does not exist (404)."""


class DomoRateLimitError(DomoError):
    """Raised when the Domo API returns 429 Too Many Requests."""
