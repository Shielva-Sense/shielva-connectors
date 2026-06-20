"""
RingCentral connector exceptions.
"""


class RingCentralError(Exception):
    """Base exception for all RingCentral connector errors."""

    def __init__(self, message: str = "", status_code: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r}, status_code={self.status_code})"


class RingCentralAuthError(RingCentralError):
    """Raised on 401/403 responses — bad or expired credentials."""


class RingCentralNetworkError(RingCentralError):
    """Raised on 5xx responses or transport-level failures."""


class RingCentralNotFoundError(RingCentralError):
    """Raised on 404 responses."""


class RingCentralRateLimitError(RingCentralError):
    """Raised on 429 responses — caller should back off."""
