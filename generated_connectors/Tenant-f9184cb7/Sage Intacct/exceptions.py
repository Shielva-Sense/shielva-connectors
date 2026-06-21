"""Sage Intacct connector exception hierarchy.

Every exception extends :class:`SageIntacctError` and carries:

  * ``status_code`` — HTTP status (0 when the failure is XML-level).
  * ``response_body`` — parsed body / XML error block (always a dict).

These are the only exception classes ``connector.py`` is allowed to catch by
type; raw ``httpx`` / ``xml.etree`` errors are translated by the HTTP client
and the parser respectively.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class SageIntacctError(Exception):
    """Base exception for all Sage Intacct connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code: int = status_code
        self.response_body: Dict[str, Any] = response_body or {}


class SageIntacctAuthError(SageIntacctError):
    """Raised on HTTP 401, HTTP 403, or any ``XL03*`` XML error code.

    Triggered when the Web Services sender pair, the Intacct user pair, or
    the company id is rejected. Surfaces to the gateway as
    ``AuthStatus.TOKEN_EXPIRED`` (transport-level 401) or
    ``AuthStatus.INVALID_CREDENTIALS`` (XML-level XL03*).
    """


class SageIntacctBadRequestError(SageIntacctError):
    """Raised on HTTP 400 — the gateway rejected the envelope before parsing."""


class SageIntacctNotFoundError(SageIntacctError):
    """Raised on HTTP 404 — gateway URL incorrect or sandbox decommissioned."""


class SageIntacctRateLimitError(SageIntacctError):
    """Raised when the HTTP client exhausts retries against 429 responses.

    ``retry_after_s`` is the last suggested wait derived from the
    ``Retry-After`` header (defaults to 5 s).
    """

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s: float = retry_after_s


class SageIntacctNetworkError(SageIntacctError):
    """Raised on transport-level errors (DNS / timeout / connection refused)
    or repeated 5xx after retries have been exhausted."""


class SageIntacctValidationError(SageIntacctError):
    """Raised when the XML payload is rejected by Intacct for validation
    reasons (missing required field, invalid object name, bad query syntax)
    or when caller-supplied input fails local pre-flight validation."""
