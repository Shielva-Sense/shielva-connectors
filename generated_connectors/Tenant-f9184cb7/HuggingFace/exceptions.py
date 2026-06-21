"""HuggingFace connector exception hierarchy."""
from typing import Any, Dict, Optional


class HuggingFaceError(Exception):
    """Base for all HuggingFace-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class HuggingFaceAuthError(HuggingFaceError):
    """401 / 403 — API token invalid, missing, or lacks scope."""


class HuggingFaceBadRequestError(HuggingFaceError):
    """400 — malformed request body."""


class HuggingFaceNotFound(HuggingFaceError):
    """404 — model / dataset / space / endpoint not found."""


class HuggingFaceRateLimitError(HuggingFaceError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class HuggingFaceModelLoadingError(HuggingFaceError):
    """503 — Inference API returns this while a model warms up.

    Carries ``estimated_time`` (seconds the API expects the load to take) so
    callers can sleep that long before retrying.
    """

    def __init__(self, message: str, estimated_time: float = 20.0):
        super().__init__(message, status_code=503)
        self.estimated_time = float(estimated_time)


class HuggingFaceServerError(HuggingFaceError):
    """5xx — provider-side outage; retry candidate."""


class HuggingFaceNetworkError(HuggingFaceError):
    """Transport-level failure (timeout, DNS, connection reset)."""


# Back-compat alias for older code that imports the older spelling.
HuggingFaceAPIError = HuggingFaceServerError
HuggingFaceNotFoundError = HuggingFaceNotFound
