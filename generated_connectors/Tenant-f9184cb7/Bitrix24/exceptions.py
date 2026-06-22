"""Bitrix24 connector exception hierarchy."""


class Bitrix24Error(Exception):
    """Base for all Bitrix24-connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class Bitrix24AuthError(Bitrix24Error):
    """401 / 403 — webhook URL invalid, token expired, or insufficient scope."""


class Bitrix24BadRequestError(Bitrix24Error):
    """400 — malformed request body or unknown method."""


class Bitrix24NotFoundError(Bitrix24Error):
    """404 — resource (lead / contact / deal / task / ...) not found."""


class Bitrix24ConflictError(Bitrix24Error):
    """409 — duplicate / state conflict."""


class Bitrix24RateLimitError(Bitrix24Error):
    """429 / QUERY_LIMIT_EXCEEDED — Bitrix24 quota exceeded. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class Bitrix24ServerError(Bitrix24Error):
    """5xx — provider-side outage; retry candidate."""


# ── Back-compat aliases ──────────────────────────────────────────────────────
# Older callers (and shielva-connectors core) may still import these names.
Bitrix24NetworkError = Bitrix24ServerError
Bitrix24NotFound = Bitrix24NotFoundError
Bitrix24APIError = Bitrix24Error
Bitrix24ConnectorError = Bitrix24Error
