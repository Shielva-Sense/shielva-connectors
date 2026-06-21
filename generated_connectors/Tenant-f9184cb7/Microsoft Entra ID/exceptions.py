"""Microsoft Entra ID connector exception hierarchy."""
from typing import Any, Dict, Optional


class EntraIdError(Exception):
    """Base exception for all Entra ID connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class EntraIdAuthError(EntraIdError):
    """401 / 403 — token expired / invalid, or app lacks required Graph permission."""


class EntraIdBadRequestError(EntraIdError):
    """400 — malformed request body or invalid $filter / $select."""


class EntraIdNotFound(EntraIdError):
    """404 — Graph resource not found."""


class EntraIdConflictError(EntraIdError):
    """409 — conflict (e.g. duplicate userPrincipalName)."""


class EntraIdRateLimitError(EntraIdError):
    """429 — throttled. retry_after_s is the suggested wait (parsed from Retry-After header)."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class EntraIdServerError(EntraIdError):
    """5xx — Graph-side outage; retry candidate."""


class EntraIdNetworkError(EntraIdError):
    """Transport / DNS / TLS error before getting an HTTP response."""


# Back-compat alias for callers that import the older name.
EntraIDError = EntraIdError
EntraIDAuthError = EntraIdAuthError
EntraIDNotFound = EntraIdNotFound
EntraIDNetworkError = EntraIdNetworkError
