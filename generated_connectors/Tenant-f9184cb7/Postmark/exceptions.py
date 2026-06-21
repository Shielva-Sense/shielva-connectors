"""Postmark connector exception hierarchy.

All connector-raised errors derive from ``PostmarkError`` so callers can catch a
single base type. HTTP-status-specific subclasses give finer control to the
gateway / sync layer.
"""


class PostmarkError(Exception):
    """Base for all Postmark-connector errors.

    Carries the HTTP ``status_code`` (when the failure is HTTP-origin) and the
    parsed ``response_body`` (the JSON dict Postmark returned, when present).
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class PostmarkAuthError(PostmarkError):
    """401 / 403 — server_token or account_token invalid, missing, or lacks scope."""


class PostmarkBadRequestError(PostmarkError):
    """400 / 422 with a non-special ErrorCode — malformed request body."""


class PostmarkNotFoundError(PostmarkError):
    """404 — resource not found."""


class PostmarkConflictError(PostmarkError):
    """409 — duplicate / state conflict (e.g. template alias already exists)."""


class PostmarkPreconditionError(PostmarkError):
    """428 — precondition required (revision mismatch)."""


class PostmarkRateLimitError(PostmarkError):
    """429 — rate limited. ``retry_after_s`` is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class PostmarkServerError(PostmarkError):
    """5xx — provider-side outage; retry candidate."""


class PostmarkInactiveRecipient(PostmarkError):
    """Postmark refused delivery because the recipient is suppressed.

    Postmark returns this as ``HTTP 422`` with JSON ``{"ErrorCode": 406, ...}``.
    The connector surfaces it as a distinct exception so the caller can
    deactivate / re-route without re-parsing JSON.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 422,
        response_body: dict | None = None,
    ):
        super().__init__(message, status_code=status_code, response_body=response_body)


# ── Back-compat aliases ────────────────────────────────────────────────────────
# Older callers may import these names; keep them resolvable.
PostmarkNetworkError = PostmarkServerError
PostmarkNotFound = PostmarkNotFoundError
