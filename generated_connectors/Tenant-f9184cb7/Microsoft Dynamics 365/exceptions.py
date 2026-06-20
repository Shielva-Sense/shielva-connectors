from __future__ import annotations


class Dynamics365Error(Exception):
    """Base exception for all Microsoft Dynamics 365 connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code

    def __str__(self) -> str:
        if self.status_code:
            return f"[{self.status_code}] {self.message}"
        return self.message


class Dynamics365AuthError(Dynamics365Error):
    """Raised when Microsoft / Dynamics 365 rejects the access token (401/403)."""


class Dynamics365NetworkError(Dynamics365Error):
    """Raised on transient network failures (timeouts, connection errors, 5xx)."""


class Dynamics365NotFoundError(Dynamics365Error):
    """Raised when a requested Dynamics 365 resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str = "") -> None:
        msg = f"{resource} '{resource_id}' not found" if resource_id else f"{resource} not found"
        super().__init__(msg, status_code=404, code="NOT_FOUND")


class Dynamics365RateLimitError(Dynamics365Error):
    """Raised on 429 Too Many Requests from the Dataverse API."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after
