"""Document360 connector exception hierarchy."""
from typing import Optional


class Document360Error(Exception):
    """Base exception for all Document360 connector errors."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: Optional[dict] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class Document360AuthError(Document360Error):
    """401 / 403 — api_token invalid, missing, or lacks permissions."""


class Document360BadRequestError(Document360Error):
    """400 — malformed request body."""


class Document360NotFound(Document360Error):
    """404 — Document360 resource (project, version, category, article, drive file) not found."""


class Document360ConflictError(Document360Error):
    """409 — duplicate / state conflict (e.g. duplicate article slug)."""


class Document360RateLimitError(Document360Error):
    """429 — rate limited and retries exhausted."""

    def __init__(self, message: str = "", retry_after_s: float = 5.0, **kwargs):
        super().__init__(message, **kwargs)
        self.retry_after_s = retry_after_s


class Document360NetworkError(Document360Error):
    """Transient network failures, timeouts, or 5xx after retries are exhausted."""


# Back-compat aliases for older imports
Document360NotFoundError = Document360NotFound
Document360ServerError = Document360NetworkError
