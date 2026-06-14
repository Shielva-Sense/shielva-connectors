"""Custom exception hierarchy for the Gmail connector."""


class GmailBaseError(Exception):
    """Base exception for all Gmail connector errors."""


class GmailAuthError(GmailBaseError):
    """Raised on 401/403 responses — maps to MISSING_CREDENTIALS or TOKEN_EXPIRED."""


class GmailRateLimitError(GmailBaseError):
    """Raised on 429 responses — triggers exponential backoff in the caller."""


class GmailAPIError(GmailBaseError):
    """Raised on 5xx responses or transport-level failures."""
