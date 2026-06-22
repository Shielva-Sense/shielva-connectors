"""Custom exceptions for the ADP connector."""


class ADPError(Exception):
    """Base exception for all ADP connector errors."""


class ADPAuthError(ADPError):
    """Raised when authentication / mTLS / token mint fails or returns 401."""


class ADPNetworkError(ADPError):
    """Raised on transient network failures, 5xx upstreams, or rate limiting (429)."""


class ADPNotFound(ADPError):
    """Raised when the ADP API returns 404 for a resource (worker / pay statement etc.)."""


class ADPAPIError(ADPError):
    """Raised for any other unexpected ADP API error (4xx/5xx not classified above)."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}
