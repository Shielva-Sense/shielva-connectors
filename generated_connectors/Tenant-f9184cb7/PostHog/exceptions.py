"""PostHog connector exception hierarchy."""


class PostHogError(Exception):
    """Base for all PostHog-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class PostHogAuthError(PostHogError):
    """401 / 403 — personal API key invalid, missing, or lacks permissions."""


class PostHogBadRequestError(PostHogError):
    """400 — malformed request body."""


class PostHogNotFoundError(PostHogError):
    """404 — project / flag / cohort / person not found."""


class PostHogConflictError(PostHogError):
    """409 — duplicate / state conflict (e.g. feature-flag key collision)."""


class PostHogRateLimitError(PostHogError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class PostHogServerError(PostHogError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
PostHogNetworkError = PostHogServerError
PostHogNotFound = PostHogNotFoundError
