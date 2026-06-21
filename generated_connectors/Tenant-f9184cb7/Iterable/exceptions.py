"""Iterable connector exception hierarchy.

Mirrors the Wix gold-standard shape: a `IterableError` base carrying
`status_code` + `response_body`, with one specialised subclass per HTTP
status family. Back-compat aliases preserve the names used by older code
(`IterableNotFound`, `IterableNetworkError` for transport errors).
"""
from __future__ import annotations

from typing import Optional


class IterableError(Exception):
    """Base for all Iterable-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class IterableAuthError(IterableError):
    """401 / 403 — API key invalid, missing, or lacks the requested scope."""


class IterableBadRequestError(IterableError):
    """400 — malformed request body or missing required field."""


class IterableNotFoundError(IterableError):
    """404 — resource not found (user, list, template, campaign…)."""


class IterableConflictError(IterableError):
    """409 — duplicate / state conflict (e.g. list name already in use)."""


class IterableRateLimitError(IterableError):
    """429 — caller hit Iterable's per-second / per-minute rate cap.

    `retry_after_s` is the suggested wait, taken from the `Retry-After`
    response header when present.
    """

    def __init__(self, message: str, retry_after_s: float = 5.0) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class IterableServerError(IterableError):
    """5xx — provider-side outage; retry candidate."""


class IterableNetworkError(IterableError):
    """Transport-level error (timeout, DNS, TLS, dropped connection)."""


# ── Back-compat aliases — older callers import these names ───────────────
IterableNotFound = IterableNotFoundError
