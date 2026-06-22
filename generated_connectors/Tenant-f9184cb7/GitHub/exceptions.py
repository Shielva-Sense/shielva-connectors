from __future__ import annotations


class GitHubError(Exception):
    """Base exception for all GitHub connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class GitHubAuthError(GitHubError):
    """Raised when GitHub rejects the token (401/403)."""


class GitHubRateLimitError(GitHubError):
    """Raised on 429 Too Many Requests or when X-RateLimit-Remaining reaches 0."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class GitHubNotFoundError(GitHubError):
    """Raised when a requested GitHub resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )


class GitHubNetworkError(GitHubError):
    """Raised on transient network failures (timeouts, connection errors)."""


class GitHubServerError(GitHubError):
    """Raised on 5xx responses from GitHub."""
