"""Aircall connector exception hierarchy."""


class AircallError(Exception):
    """Base for all Aircall-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class AircallAuthError(AircallError):
    """401 / 403 — API id/token invalid, missing, or lacks permissions."""


class AircallBadRequestError(AircallError):
    """400 — malformed request body or query string."""


class AircallNotFoundError(AircallError):
    """404 — resource not found."""


class AircallConflictError(AircallError):
    """409 — duplicate or state conflict (e.g. contact email already exists)."""


class AircallRateLimitError(AircallError):
    """429 — Aircall rate limit exceeded (default 60/min)."""

    def __init__(self, message: str, status_code: int = 429, response_body: dict | None = None, retry_after_s: float = 5.0):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class AircallServerError(AircallError):
    """5xx — provider-side outage; retry candidate."""


class AircallNetworkError(AircallError):
    """Transport-level failure (timeout, DNS, connection reset)."""


# Back-compat aliases for older code paths.
AircallNotFound = AircallNotFoundError
