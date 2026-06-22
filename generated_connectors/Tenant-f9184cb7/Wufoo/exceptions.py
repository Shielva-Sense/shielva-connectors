"""Wufoo connector exception hierarchy."""


class WufooError(Exception):
    """Base for all Wufoo-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class WufooAuthError(WufooError):
    """401 / 403 — API key invalid, missing, or lacks permissions."""


class WufooBadRequestError(WufooError):
    """400 — malformed request body / invalid Wufoo field IDs."""


class WufooNotFoundError(WufooError):
    """404 — form, entry, report, or webhook not found."""


class WufooConflictError(WufooError):
    """409 — duplicate / state conflict."""


class WufooRateLimitError(WufooError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class WufooServerError(WufooError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
WufooNetworkError = WufooServerError
WufooNotFound = WufooNotFoundError
