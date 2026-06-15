"""Custom exception hierarchy for the Gmail connector."""


class ConnectorError(Exception):
    """Base exception for all Gmail connector errors."""


class ConnectorAuthError(ConnectorError):
    """Raised on HTTP 401 — token invalid or missing."""


class ConnectorPermissionError(ConnectorError):
    """Raised on HTTP 403 — insufficient scope or access denied."""


class ConnectorNotFoundError(ConnectorError):
    """Raised on HTTP 404 — resource not found."""


class ConnectorRateLimitError(ConnectorError):
    """Raised on HTTP 429 — rate limit exceeded; triggers retry backoff."""
