"""Miro connector — typed exception hierarchy."""
from __future__ import annotations


class MiroError(Exception):
    """Base exception for all Miro connector errors."""


class MiroAuthError(MiroError):
    """Raised when the API returns 401 or 403 (invalid / expired token)."""


class MiroNetworkError(MiroError):
    """Raised on network-level failures or 5xx responses."""


class MiroNotFoundError(MiroError):
    """Raised when the API returns 404 (resource does not exist)."""


class MiroRateLimitError(MiroError):
    """Raised when the API returns 429 (rate limit exceeded)."""
