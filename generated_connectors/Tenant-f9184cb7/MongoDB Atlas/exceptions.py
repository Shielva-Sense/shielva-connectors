"""MongoDB Atlas connector exception hierarchy."""
from typing import Any, Dict, Optional


class MongoDBAtlasError(Exception):
    """Base for all MongoDB Atlas connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class MongoDBAtlasAuthError(MongoDBAtlasError):
    """401 / 403 — Digest credentials invalid, expired, or lacking permission."""


class MongoDBAtlasBadRequestError(MongoDBAtlasError):
    """400 — malformed request body or invalid parameter."""


class MongoDBAtlasNotFoundError(MongoDBAtlasError):
    """404 — Atlas org / project / cluster / user not found."""


class MongoDBAtlasConflictError(MongoDBAtlasError):
    """409 — duplicate resource (cluster name in use, etc.)."""


class MongoDBAtlasRateLimitError(MongoDBAtlasError):
    """429 — Atlas API rate limit exceeded. retry_after_s carries the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class MongoDBAtlasServerError(MongoDBAtlasError):
    """5xx — Atlas-side outage; retry candidate."""


class MongoDBAtlasNetworkError(MongoDBAtlasError):
    """Transport failure (DNS, timeout, connection refused)."""


# Back-compat aliases for callers using the older names.
MongoDBAtlasNotFound = MongoDBAtlasNotFoundError
