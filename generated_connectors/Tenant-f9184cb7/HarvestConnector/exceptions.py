"""Harvest connector exception hierarchy."""


class HarvestError(Exception):
    """Base for all Harvest-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class HarvestAuthError(HarvestError):
    """401 / 403 — PAT invalid, missing, or lacks scope."""


class HarvestBadRequestError(HarvestError):
    """400 / 422 — malformed request body or validation failure."""


class HarvestNotFoundError(HarvestError):
    """404 — resource not found."""


class HarvestConflictError(HarvestError):
    """409 — duplicate / state conflict."""


class HarvestRateLimitError(HarvestError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class HarvestServerError(HarvestError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
HarvestNetworkError = HarvestServerError
HarvestNotFound = HarvestNotFoundError
