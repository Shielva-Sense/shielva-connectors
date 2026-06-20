"""Copper CRM connector exceptions."""


class CopperError(Exception):
    """Base exception for all Copper connector errors."""

    def __init__(self, message: str = "", status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r}, status_code={self.status_code!r})"


class CopperAuthError(CopperError):
    """Raised when authentication fails (401 Unauthorized or 403 Forbidden)."""

    def __init__(self, message: str = "Authentication failed") -> None:
        super().__init__(message, status_code=401)


class CopperNetworkError(CopperError):
    """Raised when a network-level error occurs (connection refused, timeout, etc.)."""

    def __init__(self, message: str = "Network error") -> None:
        super().__init__(message, status_code=None)


class CopperNotFoundError(CopperError):
    """Raised when the requested resource is not found (404)."""

    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__(message, status_code=404)


class CopperRateLimitError(CopperError):
    """Raised when the Copper API rate limit is exceeded (429)."""

    def __init__(self, message: str = "Rate limit exceeded") -> None:
        super().__init__(message, status_code=429)
