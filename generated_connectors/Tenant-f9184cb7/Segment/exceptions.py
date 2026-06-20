from __future__ import annotations


class SegmentError(Exception):
    """Base exception for all Segment connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class SegmentAuthError(SegmentError):
    """Raised when Segment rejects the access token (401/403)."""


class SegmentInvalidTokenError(SegmentAuthError):
    """Raised when the access token is missing or clearly malformed."""


class SegmentRateLimitError(SegmentError):
    """Raised on 429 Too Many Requests from Segment."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class SegmentNotFoundError(SegmentError):
    """Raised when a requested Segment resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class SegmentNetworkError(SegmentError):
    """Raised on transient network failures (timeouts, connection errors)."""


class SegmentServerError(SegmentError):
    """Raised on 5xx responses from Segment."""
