"""Weaviate connector exception hierarchy."""


class WeaviateError(Exception):
    """Base for all Weaviate-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class WeaviateAuthError(WeaviateError):
    """401 / 403 — API key invalid, missing, or lacks RBAC scope."""


class WeaviateBadRequestError(WeaviateError):
    """400 — malformed request body."""


class WeaviateNotFoundError(WeaviateError):
    """404 — class, object, or tenant not found."""


class WeaviateConflictError(WeaviateError):
    """409 — duplicate object id or class already exists."""


class WeaviateValidationError(WeaviateError):
    """422 — schema or property validation failure."""


class WeaviateRateLimitError(WeaviateError):
    """429 — rate limited. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class WeaviateServerError(WeaviateError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases (kept stable for older imports).
WeaviateNetworkError = WeaviateServerError
WeaviateNotFound = WeaviateNotFoundError
