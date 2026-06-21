"""Qdrant connector exception hierarchy."""


class QdrantError(Exception):
    """Base for all Qdrant-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class QdrantAuthError(QdrantError):
    """401 / 403 — api-key invalid, missing, or lacks permissions."""


class QdrantBadRequestError(QdrantError):
    """400 — malformed request body / wrong vector dim / unknown field."""


class QdrantNotFoundError(QdrantError):
    """404 — collection or point not found."""


class QdrantConflictError(QdrantError):
    """409 — collection already exists / shard conflict."""


class QdrantRateLimitError(QdrantError):
    """429 — Cloud-tier rate limit. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class QdrantServerError(QdrantError):
    """5xx — cluster outage / shard failover; retry candidate."""


# Back-compat aliases for older code that imports these names.
QdrantNetworkError = QdrantServerError
QdrantNotFound = QdrantNotFoundError
