"""YouTrack connector exception hierarchy."""


class YouTrackError(Exception):
    """Base for all YouTrack-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class YouTrackAuthError(YouTrackError):
    """401 / 403 — permanent token invalid, missing, or lacks permissions."""


class YouTrackBadRequestError(YouTrackError):
    """400 — malformed request body or query."""


class YouTrackNotFoundError(YouTrackError):
    """404 — resource not found."""


class YouTrackConflictError(YouTrackError):
    """409 — duplicate / state conflict."""


class YouTrackPreconditionError(YouTrackError):
    """428 — precondition required (revision mismatch)."""


class YouTrackRateLimitError(YouTrackError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0, response_body: dict | None = None):
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after_s = retry_after_s


class YouTrackServerError(YouTrackError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
YouTrackNetworkError = YouTrackServerError
YouTrackNotFound = YouTrackNotFoundError
