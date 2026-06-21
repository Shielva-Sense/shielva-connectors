"""Custom exceptions for the Elasticsearch connector."""


class ElasticsearchError(Exception):
    """Base exception for all Elasticsearch connector errors."""

    def __init__(self, message: str, status_code: int = 0, response_body: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class ElasticsearchAuthError(ElasticsearchError):
    """Raised on 401/403 — invalid API key or basic-auth credentials."""


class ElasticsearchNetworkError(ElasticsearchError):
    """Raised on transport errors (DNS / connect / TLS / timeout)."""


class ElasticsearchNotFound(ElasticsearchError):
    """Raised on 404 — index or document does not exist."""


class ElasticsearchRateLimitError(ElasticsearchError):
    """Raised on 429 — cluster rate-limit / circuit breaker."""
