"""n8n connector exception hierarchy.

Every error raised by the connector is a subclass of ``N8nError`` and carries
the originating HTTP ``status_code`` + the JSON-decoded ``response_body`` for
debugging.
"""
from typing import Any, Dict, Optional


class N8nError(Exception):
    """Base for all n8n-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class N8nAuthError(N8nError):
    """401 / 403 — API key invalid, missing, or lacks permissions."""


class N8nBadRequestError(N8nError):
    """400 — malformed request body / query."""


class N8nNotFound(N8nError):
    """404 — workflow / execution / credential / tag not found."""


class N8nConflictError(N8nError):
    """409 — duplicate / state conflict."""


class N8nRateLimitError(N8nError):
    """429 — rate limited. ``retry_after_s`` is the suggested wait."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_body: Optional[Dict[str, Any]] = None,
        retry_after_s: float = 5.0,
    ):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class N8nServerError(N8nError):
    """5xx — provider-side outage; retry candidate."""


class N8nNetworkError(N8nError):
    """Transport-level failure after retries exhausted (timeout / DNS / RST)."""


class N8nAPIError(N8nError):
    """Catch-all for other 4xx unmapped by the typed branches above."""


# Back-compat aliases for older call sites.
N8nNotFoundError = N8nNotFound
