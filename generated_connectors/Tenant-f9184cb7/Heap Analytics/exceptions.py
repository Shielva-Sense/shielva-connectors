from __future__ import annotations


class HeapError(Exception):
    """Base exception for all Heap Analytics connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class HeapAuthError(HeapError):
    """Raised when Heap rejects the API key (401/403)."""


class HeapNetworkError(HeapError):
    """Raised on transient network failures (timeouts, connection errors)."""


class HeapNotFoundError(HeapError):
    """Raised when a requested Heap resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class HeapRateLimitError(HeapError):
    """Raised on 429 Too Many Requests from Heap."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class HeapServerError(HeapError):
    """Raised on 5xx responses from Heap."""
