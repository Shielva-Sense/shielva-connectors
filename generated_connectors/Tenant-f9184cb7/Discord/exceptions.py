"""Discord connector exception hierarchy."""


class DiscordError(Exception):
    """Base for all Discord-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class DiscordAuthError(DiscordError):
    """401 / 403 — Bot token revoked, OAuth token expired, or missing permissions."""


class DiscordBadRequestError(DiscordError):
    """400 — malformed request body."""


class DiscordNotFoundError(DiscordError):
    """404 — resource not found."""


class DiscordConflictError(DiscordError):
    """409 — duplicate / state conflict."""


class DiscordRateLimitError(DiscordError):
    """429 — rate limited. ``retry_after`` is the Discord-supplied wait (seconds)."""

    def __init__(self, message: str, retry_after: float = 5.0, status_code: int = 429,
                 response_body: dict | None = None):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after = retry_after


class DiscordServerError(DiscordError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imported these names.
DiscordNetworkError = DiscordServerError
DiscordNotFound = DiscordNotFoundError
DiscordAPIError = DiscordError
