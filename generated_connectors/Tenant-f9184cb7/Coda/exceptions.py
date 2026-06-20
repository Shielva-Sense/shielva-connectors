"""Coda connector — custom exception hierarchy."""
from __future__ import annotations


class CodaError(Exception):
    """Base exception for all Coda connector errors."""


class CodaAuthError(CodaError):
    """Raised on authentication failures — invalid or missing API token."""


class CodaNetworkError(CodaError):
    """Raised on connection / timeout failures."""


class CodaRateLimitError(CodaError):
    """Raised when Coda API returns 429 Too Many Requests."""


class CodaNotFoundError(CodaError):
    """Raised when a requested resource does not exist (404)."""
