"""Microsoft Teams connector — custom exception hierarchy."""
from __future__ import annotations


class MicrosoftTeamsError(Exception):
    """Base exception for all Microsoft Teams connector errors."""


class MicrosoftTeamsAuthError(MicrosoftTeamsError):
    """Raised on authentication failures — invalid token, expired token, insufficient permissions."""


class MicrosoftTeamsNetworkError(MicrosoftTeamsError):
    """Raised on connection / timeout failures."""


class MicrosoftTeamsNotFoundError(MicrosoftTeamsError):
    """Raised when a requested resource does not exist (team not found, channel not found)."""


class MicrosoftTeamsRateLimitError(MicrosoftTeamsError):
    """Raised when Microsoft Graph API returns 429 Too Many Requests."""
