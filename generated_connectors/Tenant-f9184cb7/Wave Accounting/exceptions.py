"""Wave Accounting connector exception hierarchy."""


class WaveError(Exception):
    """Base for all Wave-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class WaveAuthError(WaveError):
    """401 / 403 — access token invalid, revoked, or missing required scope."""


class WaveBadRequestError(WaveError):
    """400 — malformed GraphQL query or variables."""


class WaveValidationError(WaveError):
    """Client-side validation error (e.g. missing required argument)."""


class WaveNotFoundError(WaveError):
    """404 — resource not found."""


class WaveRateLimitError(WaveError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0, response_body: dict | None = None):
        super().__init__(message, status_code=429, response_body=response_body)
        self.retry_after_s = retry_after_s


class WaveServerError(WaveError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
WaveNetworkError = WaveServerError
WaveNotFound = WaveNotFoundError
