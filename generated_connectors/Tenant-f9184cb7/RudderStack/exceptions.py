"""Rudderstack connector exception hierarchy."""


class RudderstackError(Exception):
    """Base for all Rudderstack-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class RudderstackAuthError(RudderstackError):
    """401 / 403 — write_key or PAT invalid / lacking permissions."""


class RudderstackBadRequestError(RudderstackError):
    """400 — malformed request body."""


class RudderstackNotFoundError(RudderstackError):
    """404 — source/destination/connection/profile not found."""

    def __init__(self, message: str, status_code: int = 404, response_body: dict | None = None, resource_id: str = ""):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.resource_id = resource_id


class RudderstackConflictError(RudderstackError):
    """409 — duplicate / state conflict."""


class RudderstackRateLimitError(RudderstackError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0, response_body: dict | None = None):
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after_s = retry_after_s


class RudderstackServerError(RudderstackError):
    """5xx — provider-side outage; retry candidate."""


# ── Back-compat aliases for older code that imports the legacy names ─────────
RudderstackNetworkError = RudderstackServerError
RudderstackNotFound = RudderstackNotFoundError

# Legacy capitalisation aliases ("RudderStack" with a capital S) — older
# downstream code (and the previous version of this connector) imported these.
RudderStackError = RudderstackError
RudderStackAuthError = RudderstackAuthError
RudderStackBadRequestError = RudderstackBadRequestError
RudderStackNotFoundError = RudderstackNotFoundError
RudderStackConflictError = RudderstackConflictError
RudderStackRateLimitError = RudderstackRateLimitError
RudderStackServerError = RudderstackServerError
RudderStackNetworkError = RudderstackNetworkError
RudderStackNotFound = RudderstackNotFoundError
