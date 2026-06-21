"""Dropbox Sign connector exception hierarchy.

Mirrors the Wix exception layout so the platform's gateway can rely on a
consistent status-code → exception mapping across connectors.
"""
from typing import Any, Dict, Optional


class DropboxSignError(Exception):
    """Base for every Dropbox Sign-connector error.

    Carries the raw HTTP status_code and decoded response_body so the
    gateway can surface provider context to the operator.
    """

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class DropboxSignAuthError(DropboxSignError):
    """401 / 403 — API key invalid, missing, or lacks permission."""


class DropboxSignBadRequestError(DropboxSignError):
    """400 — malformed request body."""


class DropboxSignNotFoundError(DropboxSignError):
    """404 — signature request / template / resource does not exist."""


class DropboxSignConflictError(DropboxSignError):
    """409 — duplicate / state conflict (e.g. cancelling an already-finalized request)."""


class DropboxSignRateLimitError(DropboxSignError):
    """429 — rate limited. `retry_after_s` is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class DropboxSignServerError(DropboxSignError):
    """5xx — provider-side outage; retry candidate."""


class DropboxSignNetworkError(DropboxSignError):
    """Transport failure (timeouts, DNS, connection reset) after retries are exhausted."""


# Back-compat aliases for the older imports.
DropboxSignNotFound = DropboxSignNotFoundError
