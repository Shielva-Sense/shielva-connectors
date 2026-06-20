from __future__ import annotations


class PandaDocError(Exception):
    """Base exception for all PandaDoc connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class PandaDocAuthError(PandaDocError):
    """Raised when PandaDoc rejects credentials (401/403)."""


class PandaDocNetworkError(PandaDocError):
    """Raised on transient network failures (timeouts, connection errors)."""


class PandaDocNotFoundError(PandaDocError):
    """Raised when a requested PandaDoc resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_not_found",
        )


class PandaDocRateLimitError(PandaDocError):
    """Raised on 429 Too Many Requests from PandaDoc."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class PandaDocServerError(PandaDocError):
    """Raised on 5xx responses from PandaDoc."""
