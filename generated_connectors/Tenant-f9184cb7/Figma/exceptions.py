"""Figma connector — custom exception hierarchy."""
from __future__ import annotations


class FigmaError(Exception):
    """Base exception for all Figma connector errors."""


class FigmaAuthError(FigmaError):
    """Raised on authentication failures — invalid or missing personal access token."""


class FigmaNetworkError(FigmaError):
    """Raised on connection / timeout failures."""


class FigmaRateLimitError(FigmaError):
    """Raised when Figma API returns 429 Too Many Requests."""


class FigmaNotFoundError(FigmaError):
    """Raised when a requested resource does not exist (404)."""
