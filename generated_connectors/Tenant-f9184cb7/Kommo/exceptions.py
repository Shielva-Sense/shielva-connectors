"""Kommo connector exception hierarchy."""


class KommoError(Exception):
    """Base for all Kommo-connector errors.

    Carries the upstream HTTP status_code and parsed response_body so the
    orchestrator (connector.py) can map them to ConnectorHealth / AuthStatus.
    """

    def __init__(
        self,
        message: str = "",
        status_code: int = 0,
        response_body: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class KommoAuthError(KommoError):
    """401 / 403 — long-lived token invalid, revoked, or lacks scope."""


class KommoBadRequestError(KommoError):
    """400 — malformed request body."""


class KommoNotFoundError(KommoError):
    """404 — resource not found."""


class KommoConflictError(KommoError):
    """409 — duplicate / state conflict."""


class KommoRateLimitError(KommoError):
    """429 — rate limited."""

    def __init__(self, message: str, retry_after_s: float = 1.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class KommoServerError(KommoError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older callers that imported these names.
KommoNetworkError = KommoServerError
KommoNotFound = KommoNotFoundError
