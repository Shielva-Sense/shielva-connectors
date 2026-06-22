"""Outlook Calendar connector — custom exception hierarchy."""
from __future__ import annotations


class OutlookCalendarError(Exception):
    """Base exception for all Outlook Calendar connector errors."""


class OutlookCalendarAuthError(OutlookCalendarError):
    """Raised on 401 / 403 — token expired or insufficient scopes."""


class OutlookCalendarNetworkError(OutlookCalendarError):
    """Raised on connection / timeout failures."""


class OutlookCalendarNotFoundError(OutlookCalendarError):
    """Raised when a requested resource does not exist (404)."""


class OutlookCalendarRateLimitError(OutlookCalendarError):
    """Raised when the Graph API returns 429."""
