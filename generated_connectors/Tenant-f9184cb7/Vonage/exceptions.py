"""Custom exception hierarchy for the Vonage connector."""

from __future__ import annotations

from typing import Any, Dict, Optional


class VonageError(Exception):
    """Base for all Vonage-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class VonageAuthError(VonageError):
    """401 / 403 from Vonage — credential or permission problem.
    Also raised for SMS / Verify envelope statuses 4 and 14."""


class VonageBadRequestError(VonageError):
    """400 — malformed payload."""


class VonageNotFoundError(VonageError):
    """404 — resource not found."""


class VonageConflictError(VonageError):
    """409 — duplicate resource."""


class VonageRateLimitError(VonageError):
    """429 — rate limited. `retry_after_s` carries the server hint."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_body: Optional[Dict[str, Any]] = None,
        retry_after_s: float = 1.0,
    ) -> None:
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class VonageServerError(VonageError):
    """5xx — provider-side outage; retry candidate."""


class VonageInsufficientFunds(VonageError):
    """402 — or envelope status 9 (insufficient credit) — account balance too low."""


class VonageConfigError(VonageError):
    """Raised when a method needs JWT credentials (application_id + private_key)
    but the connector was installed with only api_key + api_secret."""


# ── Back-compat aliases (older code imports these names) ─────────────────────

# Pre-rewrite name. Kept so any existing import sites do not break.
VonageNetworkError = VonageServerError
