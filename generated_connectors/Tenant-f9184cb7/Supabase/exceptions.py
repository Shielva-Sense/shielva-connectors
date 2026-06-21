"""Supabase connector exception hierarchy.

All HTTP-status mapping happens in `client/http_client.py::_raise_for_status`.
Connector orchestrator code catches only these typed exceptions.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class SupabaseError(Exception):
    """Base for all Supabase-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class SupabaseAuthError(SupabaseError):
    """401 / 403 — service_role key invalid, missing, or forbidden by RLS."""


class SupabaseBadRequestError(SupabaseError):
    """400 / 422 — malformed request body or Postgres constraint violation."""


class SupabaseNotFoundError(SupabaseError):
    """404 — table, row, user, bucket, or object not found."""


class SupabaseConflictError(SupabaseError):
    """409 — duplicate key / unique violation."""


class SupabaseRateLimitError(SupabaseError):
    """429 — rate limited. ``retry_after_s`` is the suggested wait."""

    def __init__(
        self,
        message: str,
        retry_after_s: float = 5.0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after_s = retry_after_s


class SupabaseServerError(SupabaseError):
    """5xx — provider-side outage; retry candidate."""


# ── Back-compat aliases for older imports ─────────────────────────────────
SupabaseNotFound = SupabaseNotFoundError
SupabaseNetworkError = SupabaseServerError
