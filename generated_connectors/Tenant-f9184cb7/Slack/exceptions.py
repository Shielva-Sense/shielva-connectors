"""Slack connector — custom exception hierarchy."""
from __future__ import annotations


class SlackError(Exception):
    """Base exception for all Slack connector errors."""


class SlackAuthError(SlackError):
    """Raised on authentication failures — invalid token, revoked token."""


class SlackNetworkError(SlackError):
    """Raised on connection / timeout failures."""


class SlackRateLimitError(SlackError):
    """Raised when Slack API returns ratelimited error."""


class SlackNotFoundError(SlackError):
    """Raised when a requested resource does not exist (channel_not_found, user_not_found)."""
