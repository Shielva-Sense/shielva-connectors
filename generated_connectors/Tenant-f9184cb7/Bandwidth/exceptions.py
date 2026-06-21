"""Custom exception hierarchy for the Bandwidth connector."""


class BandwidthError(Exception):
    """Base for all Bandwidth-connector errors."""


class BandwidthAuthError(BandwidthError):
    """401 / 403 from Bandwidth — credential or permission problem."""


class BandwidthBadRequestError(BandwidthError):
    """400 — malformed payload."""


class BandwidthNotFoundError(BandwidthError):
    """404 — resource not found."""


class BandwidthConflictError(BandwidthError):
    """409 — duplicate resource (e.g. media id)."""


class BandwidthRateLimitError(BandwidthError):
    """429 — rate limited. `retry_after_s` carries the server hint."""

    def __init__(self, message: str, retry_after_s: float = 1.0):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class BandwidthServerError(BandwidthError):
    """5xx — provider-side outage; retry candidate."""
