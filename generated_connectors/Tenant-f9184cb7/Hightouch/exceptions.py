"""Hightouch connector exception hierarchy."""


class HightouchError(Exception):
    """Base for all Hightouch-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class HightouchAuthError(HightouchError):
    """401 / 403 — API token invalid or lacking scopes."""


class HightouchBadRequestError(HightouchError):
    """400 — malformed request body or query params."""


class HightouchNotFoundError(HightouchError):
    """404 — sync / model / source / destination not found."""

    def __init__(
        self,
        message: str,
        status_code: int = 404,
        response_body: dict | None = None,
        resource_id: str = "",
    ):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.resource_id = resource_id


class HightouchConflictError(HightouchError):
    """409 — duplicate slug / state conflict."""


class HightouchRateLimitError(HightouchError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(
        self,
        message: str,
        retry_after_s: float = 5.0,
        response_body: dict | None = None,
    ):
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after_s = retry_after_s


class HightouchServerError(HightouchError):
    """5xx — provider-side outage; retry candidate."""


# ── Back-compat aliases for older code that imports the legacy names ─────────
HightouchNetworkError = HightouchServerError
HightouchNotFound = HightouchNotFoundError
