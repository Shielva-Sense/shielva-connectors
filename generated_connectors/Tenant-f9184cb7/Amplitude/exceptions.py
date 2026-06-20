from __future__ import annotations


class AmplitudeError(Exception):
    """Base exception for all Amplitude connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class AmplitudeAuthError(AmplitudeError):
    """Raised when Amplitude rejects the API key / secret (401/403)."""


class AmplitudeInvalidCredentialsError(AmplitudeAuthError):
    """Raised when the api_key or api_secret is explicitly invalid."""


class AmplitudeRateLimitError(AmplitudeError):
    """Raised on 429 Too Many Requests from Amplitude."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class AmplitudeNotFoundError(AmplitudeError):
    """Raised when a requested Amplitude resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class AmplitudeNetworkError(AmplitudeError):
    """Raised on transient network failures (timeouts, connection errors)."""


class AmplitudeServerError(AmplitudeError):
    """Raised on 5xx responses from Amplitude."""
