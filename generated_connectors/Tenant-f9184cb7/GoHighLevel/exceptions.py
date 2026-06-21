"""GoHighLevel connector exception hierarchy."""
from typing import Any, Dict, Optional


class GoHighLevelError(Exception):
    """Base for all GoHighLevel-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code: int = status_code
        self.response_body: Dict[str, Any] = response_body or {}


class GoHighLevelAuthError(GoHighLevelError):
    """401 / 403 — API key invalid, missing, or lacks permissions."""


class GoHighLevelBadRequestError(GoHighLevelError):
    """400 / 422 — malformed request body."""


class GoHighLevelNotFoundError(GoHighLevelError):
    """404 — resource not found."""


class GoHighLevelConflictError(GoHighLevelError):
    """409 — duplicate / state conflict."""


class GoHighLevelRateLimitError(GoHighLevelError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_s: float = retry_after_s


class GoHighLevelServerError(GoHighLevelError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
GoHighLevelNetworkError = GoHighLevelServerError
GoHighLevelNotFound = GoHighLevelNotFoundError
