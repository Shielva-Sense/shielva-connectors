"""Odoo connector exception hierarchy."""
from typing import Any, Dict, Optional


class OdooError(Exception):
    """Base for all Odoo-connector errors."""

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


class OdooAuthError(OdooError):
    """401 / 403 / uid=False / AccessDenied / Session Expired."""


class OdooAccessError(OdooError):
    """ir.model.access denial — record-rule or model-level read/write rejected."""


class OdooBadRequestError(OdooError):
    """400 — ValidationError / UserError / malformed RPC body."""


class OdooNotFoundError(OdooError):
    """404 — missing record / id not in DB."""


class OdooRateLimitError(OdooError):
    """429 — provider-side rate limit. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class OdooServerError(OdooError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
OdooNetworkError = OdooServerError
OdooNotFound = OdooNotFoundError
