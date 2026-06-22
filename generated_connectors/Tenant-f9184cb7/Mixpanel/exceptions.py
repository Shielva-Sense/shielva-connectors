"""Mixpanel connector exception hierarchy."""
from __future__ import annotations


class MixpanelError(Exception):
    """Base exception for all Mixpanel connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code

    def __str__(self) -> str:
        if self.status_code:
            return f"[HTTP {self.status_code}] {self.message}"
        return self.message


class MixpanelAuthError(MixpanelError):
    """Raised on 401 / 403 — invalid credentials or insufficient permissions."""


class MixpanelNetworkError(MixpanelError):
    """Raised on 5xx responses or connection-level failures."""


class MixpanelNotFoundError(MixpanelError):
    """Raised on 404 — resource not found."""

    def __init__(self, resource: str, resource_id: str | int) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class MixpanelRateLimitError(MixpanelError):
    """Raised on 429 — API rate limit exceeded."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after
