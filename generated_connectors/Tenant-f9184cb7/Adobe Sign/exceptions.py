"""Adobe Sign connector exception hierarchy."""


class AdobeSignError(Exception):
    """Base for all Adobe Sign connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class AdobeSignAuthError(AdobeSignError):
    """401 / 403 — token invalid, expired, lacks scope, or wrong shard (INVALID_API_ACCESS_POINT)."""


class AdobeSignBadRequestError(AdobeSignError):
    """400 — malformed request body or invalid parameter."""


class AdobeSignNotFoundError(AdobeSignError):
    """404 — resource not found."""


class AdobeSignConflictError(AdobeSignError):
    """409 — state conflict (e.g. cancel-already-cancelled)."""


class AdobeSignRateLimitError(AdobeSignError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0, response_body: dict | None = None):
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after_s = retry_after_s


class AdobeSignServerError(AdobeSignError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
AdobeSignNetworkError = AdobeSignServerError
AdobeSignNotFound = AdobeSignNotFoundError
