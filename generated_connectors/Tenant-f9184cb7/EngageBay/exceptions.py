"""EngageBay connector exception hierarchy."""


class EngageBayError(Exception):
    """Base for all EngageBay-connector errors."""

    def __init__(self, message: str = "", status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class EngageBayAuthError(EngageBayError):
    """401 / 403 — API key invalid, missing, or lacks permissions."""


class EngageBayBadRequestError(EngageBayError):
    """400 — malformed request body."""


class EngageBayNotFoundError(EngageBayError):
    """404 — resource not found."""


class EngageBayConflictError(EngageBayError):
    """409 — duplicate / state conflict."""


class EngageBayRateLimitError(EngageBayError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class EngageBayServerError(EngageBayError):
    """5xx — provider-side outage; retry candidate."""


class EngageBayNetworkError(EngageBayError):
    """Transport-level failure (DNS, timeout, connection reset)."""


# Back-compat aliases for older code that imports these names.
EngageBayNotFound = EngageBayNotFoundError
