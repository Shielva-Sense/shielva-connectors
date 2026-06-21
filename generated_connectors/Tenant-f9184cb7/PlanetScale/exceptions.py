"""PlanetScale connector exception hierarchy.

Every typed exception carries the original HTTP status code and the parsed
response body so callers (or the gateway audit layer) can build precise
incident reports without re-parsing the wire payload.
"""
from typing import Any, Dict, Optional


class PlanetScaleError(Exception):
    """Base for all PlanetScale-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class PlanetScaleAuthError(PlanetScaleError):
    """401 / 403 — service token id/secret invalid, missing, or lacks scope."""


class PlanetScaleBadRequestError(PlanetScaleError):
    """400 / 422 — malformed request body or validation failure."""


class PlanetScaleNotFoundError(PlanetScaleError):
    """404 — organization / database / branch / deploy-request not found."""


class PlanetScaleConflictError(PlanetScaleError):
    """409 — duplicate or state conflict (e.g. branch already exists)."""


class PlanetScaleRateLimitError(PlanetScaleError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class PlanetScaleServerError(PlanetScaleError):
    """5xx — provider-side outage; retry candidate."""


# ── Back-compat aliases (older imports) ────────────────────────────────────
# Older code (and the previous connector revision) used these names. Keeping
# the aliases in place avoids cascading import breakage in callers + tests
# while the modern hierarchy above becomes the authoritative one.
PlanetScaleNetworkError = PlanetScaleServerError
PlanetScaleNotFound = PlanetScaleNotFoundError
