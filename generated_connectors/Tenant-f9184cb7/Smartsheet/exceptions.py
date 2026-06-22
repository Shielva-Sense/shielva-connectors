"""Smartsheet connector — custom exception hierarchy."""
from __future__ import annotations


class SmartsheetError(Exception):
    """Base exception for all Smartsheet connector errors."""


class SmartsheetAuthError(SmartsheetError):
    """Raised on authentication failures — invalid or expired API token."""


class SmartsheetNetworkError(SmartsheetError):
    """Raised on connection / timeout failures."""


class SmartsheetNotFoundError(SmartsheetError):
    """Raised when a requested resource does not exist (sheet not found, etc.)."""


class SmartsheetRateLimitError(SmartsheetError):
    """Raised when the Smartsheet API returns a rate-limit error (HTTP 429)."""
