"""Firebase connector exception hierarchy."""
from typing import Optional


class FirebaseError(Exception):
    """Base for all Firebase-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[dict] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class FirebaseAuthError(FirebaseError):
    """401 / 403 — token invalid, missing, or lacks IAM permissions."""


class FirebaseBadRequestError(FirebaseError):
    """400 — malformed request body / unknown field."""


class FirebaseNotFoundError(FirebaseError):
    """404 — resource (document, user, object) not found."""


class FirebaseConflictError(FirebaseError):
    """409 — duplicate email, revision mismatch, or write conflict."""


class FirebaseRateLimitError(FirebaseError):
    """429 — quota exhausted. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class FirebaseServerError(FirebaseError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older callers that imported these names.
FirebaseNetworkError = FirebaseServerError
FirebaseNotFound = FirebaseNotFoundError
