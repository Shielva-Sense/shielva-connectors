"""Mattermost connector exception hierarchy."""


class MattermostError(Exception):
    """Base for all Mattermost-connector errors."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class MattermostAuthError(MattermostError):
    """401 / 403 — token invalid, missing, or lacks permissions."""


class MattermostBadRequestError(MattermostError):
    """400 — malformed request body."""


class MattermostNotFoundError(MattermostError):
    """404 — resource not found."""


class MattermostConflictError(MattermostError):
    """409 — duplicate / state conflict."""


class MattermostRateLimitError(MattermostError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 429,
        response_body: dict | None = None,
        retry_after_s: float = 5.0,
    ):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class MattermostServerError(MattermostError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
MattermostNetworkError = MattermostServerError
MattermostNotFound = MattermostNotFoundError
