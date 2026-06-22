"""Workato connector exception hierarchy."""


class WorkatoError(Exception):
    """Base for all Workato-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class WorkatoAuthError(WorkatoError):
    """401 / 403 — API token invalid, missing, or lacks permissions."""


class WorkatoBadRequestError(WorkatoError):
    """400 — malformed request body."""


class WorkatoNotFoundError(WorkatoError):
    """404 — resource not found."""


class WorkatoConflictError(WorkatoError):
    """409 — duplicate / state conflict (e.g. start already-running recipe)."""


class WorkatoRateLimitError(WorkatoError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class WorkatoServerError(WorkatoError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
WorkatoNetworkError = WorkatoServerError
WorkatoNotFound = WorkatoNotFoundError
