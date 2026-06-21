"""Bitbucket connector exception hierarchy.

Mirrors the Wix gold-standard pattern: one root + one subclass per HTTP
status family the connector cares about. The HTTP client lives in
``client/http_client.py`` and is the sole producer of these exceptions;
``connector.py`` only catches typed instances — it never inspects status
codes inline.
"""


class BitbucketError(Exception):
    """Base for all Bitbucket-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class BitbucketAuthError(BitbucketError):
    """401 / 403 — OAuth token invalid, missing, expired, or lacks scope."""


class BitbucketBadRequestError(BitbucketError):
    """400 — malformed request body."""


class BitbucketNotFoundError(BitbucketError):
    """404 — workspace / repo / PR / issue does not exist."""


class BitbucketConflictError(BitbucketError):
    """409 — duplicate / state conflict (e.g. PR already merged)."""


class BitbucketRateLimitError(BitbucketError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_body: dict | None = None,
        retry_after_s: float = 5.0,
    ):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class BitbucketServerError(BitbucketError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
BitbucketNetworkError = BitbucketServerError
BitbucketNotFound = BitbucketNotFoundError
