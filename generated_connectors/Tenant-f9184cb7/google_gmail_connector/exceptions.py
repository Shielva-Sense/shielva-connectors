"""Custom exceptions for the Gmail connector."""


class GmailConnectorError(Exception):
    """Base exception for all Gmail connector errors."""


class GmailAuthError(GmailConnectorError):
    """Raised when authentication fails or token is invalid."""


class GmailRateLimitError(GmailConnectorError):
    """Raised when the Gmail API rate limit is exceeded."""


class GmailAPIError(GmailConnectorError):
    """Raised when the Gmail API returns an unexpected error response."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}
