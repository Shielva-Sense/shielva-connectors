from __future__ import annotations


class SnowflakeError(Exception):
    """Base exception for all Snowflake connector errors."""

    def __init__(self, message: str, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r}, status_code={self.status_code}, code={self.code!r})"


class SnowflakeAuthError(SnowflakeError):
    """Raised when Snowflake rejects credentials (401/403) or session has expired."""


class SnowflakeNetworkError(SnowflakeError):
    """Raised on transient network failures (timeouts, connection errors)."""


class SnowflakeNotFoundError(SnowflakeError):
    """Raised when a requested Snowflake resource does not exist (404)."""

    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            f"{resource} '{resource_id}' not found",
            status_code=404,
            code="resource_missing",
        )
        self.resource = resource
        self.resource_id = resource_id


class SnowflakeRateLimitError(SnowflakeError):
    """Raised on 429 Too Many Requests from Snowflake SQL API."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, status_code=429, code="rate_limit")
        self.retry_after = retry_after


class SnowflakeQueryError(SnowflakeError):
    """Raised when Snowflake returns a SQL execution error."""

    def __init__(self, message: str, sql_state: str = "", query_id: str = "") -> None:
        super().__init__(message, status_code=422, code="query_error")
        self.sql_state = sql_state
        self.query_id = query_id


class SnowflakeServerError(SnowflakeError):
    """Raised on 5xx responses from Snowflake."""
