"""Drata connector exception hierarchy."""


class DrataError(Exception):
    """Base for all Drata-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class DrataAuthError(DrataError):
    """401 / 403 — API key invalid, missing, or lacks permissions."""


class DrataBadRequestError(DrataError):
    """400 — malformed request body or query parameters."""


class DrataNotFoundError(DrataError):
    """404 — resource not found."""


class DrataConflictError(DrataError):
    """409 — duplicate / state conflict."""


class DrataRateLimitError(DrataError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class DrataServerError(DrataError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
DrataNetworkError = DrataServerError
DrataNotFound = DrataNotFoundError
