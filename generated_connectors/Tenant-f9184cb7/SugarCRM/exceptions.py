"""Custom exceptions raised by the SugarCRM connector and its HTTP client."""
from typing import Any, Dict, Optional


class SugarCRMError(Exception):
    """Base exception for all SugarCRM connector errors.

    Carries the optional ``status_code`` + ``response_body`` so callers can
    surface upstream context to the gateway when a SugarCRM REST call fails.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class SugarCRMAuthError(SugarCRMError):
    """Raised on 401 — OAuth token is invalid, revoked, or expired.

    The HTTP client raises this on the *first* 401; the connector wrapper
    catches it, refreshes the token via ``on_token_refresh()``, and retries
    the original call exactly once.
    """


class SugarCRMNetworkError(SugarCRMError):
    """Raised on transport-layer failures (connect timeout, DNS, server 5xx).

    Distinct from :class:`SugarCRMError` so :func:`helpers.utils.with_retry`
    can back off and retry without confusing it with a real API-level error.
    """


class SugarCRMRateLimitError(SugarCRMError):
    """Raised on 429 — SugarCRM rate limit exceeded.

    Carries the optional ``retry_after`` (seconds) parsed from the
    ``Retry-After`` response header for caller-driven backoff.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after
