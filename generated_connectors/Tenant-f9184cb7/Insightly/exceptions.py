"""Insightly connector exception hierarchy."""
from typing import Any, Dict, Optional


class InsightlyError(Exception):
    """Base for all Insightly-connector errors. Carries status_code + body."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class InsightlyAuthError(InsightlyError):
    """401 / 403 — API key invalid, wrong pod, or lacking permissions."""


class InsightlyBadRequestError(InsightlyError):
    """400 — malformed request body / params."""


class InsightlyNotFoundError(InsightlyError):
    """404 — resource not found."""


class InsightlyConflictError(InsightlyError):
    """409 — duplicate or state conflict."""


class InsightlyRateLimitError(InsightlyError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_body: Optional[Dict[str, Any]] = None,
        retry_after_s: float = 5.0,
    ) -> None:
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class InsightlyServerError(InsightlyError):
    """5xx — provider-side outage; retry candidate."""


# ── Back-compat aliases (older code imports these names) ─────────────────────
InsightlyNotFound = InsightlyNotFoundError
InsightlyNetworkError = InsightlyServerError
