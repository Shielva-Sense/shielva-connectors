"""Dropbox connector exception hierarchy.

All HTTP-derived errors expose ``status_code`` + ``response_body`` so callers
(and the SOC error-classification map in ``connector.py``) can branch on the
exact failure mode without parsing string messages.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class DropboxError(Exception):
    """Base for all Dropbox-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body or {}


class DropboxAuthError(DropboxError):
    """401 / 403 — token invalid / expired / missing scopes."""


class DropboxBadRequestError(DropboxError):
    """400 — malformed request body."""


class DropboxNotFoundError(DropboxError):
    """404 or 409 with ``not_found`` tag — resource does not exist."""


class DropboxConflictError(DropboxError):
    """409 (non-not_found) — path/state conflict."""


class DropboxRateLimitError(DropboxError):
    """429 — rate-limited. ``retry_after_s`` carries the Dropbox ``Retry-After``."""

    def __init__(
        self,
        message: str,
        retry_after_s: float = 5.0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after_s = float(retry_after_s)


class DropboxServerError(DropboxError):
    """5xx — provider-side outage; retry candidate."""


class DropboxNetworkError(DropboxError):
    """Transient transport-layer failure (timeout, connection reset)."""


# Back-compat aliases (the previous 147-test suite imported these names).
DropboxNotFound = DropboxNotFoundError
