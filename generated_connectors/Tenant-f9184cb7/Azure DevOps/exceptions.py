"""Azure DevOps connector exception hierarchy."""
from typing import Optional


class AzureDevOpsError(Exception):
    """Base for all Azure-DevOps-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[dict] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class AzureDevOpsAuthError(AzureDevOpsError):
    """401 / 403 — PAT invalid, expired, or lacks required scopes."""


class AzureDevOpsBadRequestError(AzureDevOpsError):
    """400 — malformed request body (e.g. JSON-patch with wrong op)."""


class AzureDevOpsNotFoundError(AzureDevOpsError):
    """404 — project, repository, work item, or build not found."""


class AzureDevOpsConflictError(AzureDevOpsError):
    """409 — state conflict (e.g. concurrent revision update on a work item)."""


class AzureDevOpsRateLimitError(AzureDevOpsError):
    """429 — TSTU-based throttle hit. retry_after_s is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class AzureDevOpsServerError(AzureDevOpsError):
    """5xx — provider-side outage; retry candidate."""


# Back-compat aliases for older code that imports these names.
AzureDevOpsNotFound = AzureDevOpsNotFoundError
AzureDevOpsNetworkError = AzureDevOpsServerError
