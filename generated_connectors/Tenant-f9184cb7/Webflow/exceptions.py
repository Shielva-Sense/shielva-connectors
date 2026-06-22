"""Webflow connector — custom exception hierarchy."""
from __future__ import annotations


class WebflowError(Exception):
    """Base exception for all Webflow connector errors."""


class WebflowAuthError(WebflowError):
    """Raised on authentication failures — invalid/expired token, unauthorized."""


class WebflowNetworkError(WebflowError):
    """Raised on connection / timeout failures."""


class WebflowNotFoundError(WebflowError):
    """Raised when a requested resource does not exist (404)."""


class WebflowRateLimitError(WebflowError):
    """Raised when the Webflow API returns 429 Too Many Requests."""
