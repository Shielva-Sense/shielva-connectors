"""Cohere connector exception hierarchy."""


class CohereError(Exception):
    """Base for all Cohere-connector errors."""

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class CohereAuthError(CohereError):
    """401 / 403 — API key invalid, missing, or lacks permissions."""


class CohereBadRequestError(CohereError):
    """400 / 422 — malformed request body or validation error."""


class CohereNotFoundError(CohereError):
    """404 — model / dataset / resource not found."""


class CohereRateLimitError(CohereError):
    """429 — rate limited.

    Cohere does not always return `Retry-After`; default to 5s.
    """

    def __init__(
        self,
        message: str = "",
        status_code: int = 429,
        response_body: dict | None = None,
        retry_after_s: float = 5.0,
    ):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class CohereServerError(CohereError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
CohereNetworkError = CohereServerError
CohereNotFound = CohereNotFoundError
