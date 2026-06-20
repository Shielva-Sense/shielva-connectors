"""Custom exceptions for the Box connector."""


class BoxError(Exception):
    """Base exception for all Box connector errors."""


class BoxAuthError(BoxError):
    """Raised when authentication fails or a token is invalid/expired."""


class BoxRateLimitError(BoxError):
    """Raised when the Box API rate limit (429) is exceeded."""


class BoxNetworkError(BoxError):
    """Raised when a network-level error prevents the API call from completing."""


class BoxNotFoundError(BoxError):
    """Raised when a requested resource (file or folder) is not found (404)."""
