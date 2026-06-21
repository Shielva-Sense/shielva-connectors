"""Algolia connector exception hierarchy.

Mapped from HTTP status codes in ``client/http_client.py::_raise_for_status``.
"""


class AlgoliaError(Exception):
    """Base exception for all Algolia connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class AlgoliaAuthError(AlgoliaError):
    """401 / 403 — API key invalid, missing, or lacks scope for the resource."""


class AlgoliaBadRequestError(AlgoliaError):
    """400 — malformed body, invalid filter syntax, unknown attribute."""


class AlgoliaNotFound(AlgoliaError):
    """404 — index, object, synonym, rule, or task does not exist."""


class AlgoliaRateLimitError(AlgoliaError):
    """429 — caller exceeded plan quota (search-ops or write-ops)."""

    def __init__(self, message: str, retry_after_s: float = 5.0, **kwargs):
        super().__init__(message, status_code=429, **kwargs)
        self.retry_after_s = retry_after_s


class AlgoliaServerError(AlgoliaError):
    """5xx — provider-side outage; host-rotation candidate."""


class AlgoliaNetworkError(AlgoliaError):
    """Raised when every host in the rotation fails (DNS / connect / 5xx)."""


# Back-compat aliases
AlgoliaNotFoundError = AlgoliaNotFound
