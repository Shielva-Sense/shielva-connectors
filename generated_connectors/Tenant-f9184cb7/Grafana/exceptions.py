"""Grafana connector exception hierarchy."""


class GrafanaError(Exception):
    """Base for all Grafana-connector errors.

    Carries `status_code` and `response_body` for downstream classification.
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


class GrafanaAuthError(GrafanaError):
    """401 / 403 — service account token invalid, revoked, or lacks scope."""


class GrafanaNotFound(GrafanaError):
    """404 — resource not found."""


class GrafanaRateLimitError(GrafanaError):
    """429 — rate limited; honour `Retry-After` header."""

    def __init__(self, message: str, retry_after_s: float = 1.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class GrafanaNetworkError(GrafanaError):
    """Transport-level failure (DNS, connect, read timeout, TLS)."""


class GrafanaAPIError(GrafanaError):
    """Unexpected HTTP error (400, 409, 5xx not retried, etc.)."""
