"""Drip connector exception hierarchy.

All exceptions extend ``DripError`` which carries the HTTP status code and the
parsed response body for downstream observability + classification.
"""


class DripError(Exception):
    """Base for all Drip-connector errors."""

    def __init__(self, message: str = "", status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class DripAuthError(DripError):
    """401 / 403 — api_token invalid, revoked, or lacks permission."""


class DripBadRequestError(DripError):
    """400 — malformed request body or invalid parameters."""


class DripNotFoundError(DripError):
    """404 — resource not found (subscriber/email/campaign id missing)."""


class DripConflictError(DripError):
    """409 — duplicate / state conflict (e.g. already subscribed)."""


class DripUnprocessableError(DripError):
    """422 — validation error on payload (Drip's typical body-level rejection)."""


class DripRateLimitError(DripError):
    """429 — rate limited. ``retry_after_s`` is the suggested wait window."""

    def __init__(self, message: str, retry_after_s: float = 5.0, response_body: dict | None = None):
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after_s = retry_after_s


class DripServerError(DripError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older call sites.
DripNetworkError = DripServerError
DripNotFound = DripNotFoundError
