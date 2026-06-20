"""Mailchimp connector — custom exception hierarchy."""
from __future__ import annotations


class MailchimpError(Exception):
    """Base exception for all Mailchimp connector errors."""


class MailchimpAuthError(MailchimpError):
    """Raised on authentication failures — invalid API key, forbidden."""


class MailchimpNetworkError(MailchimpError):
    """Raised on connection / timeout failures."""


class MailchimpRateLimitError(MailchimpError):
    """Raised when Mailchimp API returns HTTP 429 Too Many Requests."""


class MailchimpNotFoundError(MailchimpError):
    """Raised when a requested resource does not exist (HTTP 404)."""
