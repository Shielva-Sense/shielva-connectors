"""Pinecone connector exception hierarchy.

All connector-raised exceptions inherit from `PineconeError` so the orchestration
layer in `connector.py` only ever catches custom types — never bare httpx or
JSON exceptions. Each exception carries the originating `status_code` and the
parsed `response_body` for structured logging.

Back-compat aliases (`PineconeNetworkError`, `PineconeNotFound`) are preserved
so any older code that imports the legacy names keeps working without a sweep.
"""


class PineconeError(Exception):
    """Base exception for all Pinecone connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class PineconeAuthError(PineconeError):
    """401 / 403 — API key invalid, revoked, or lacks permission for this project."""


class PineconeBadRequestError(PineconeError):
    """400 — malformed body (dimension mismatch, bad filter, invalid index name, …)."""


class PineconeNotFoundError(PineconeError):
    """404 — index / collection / namespace / vector ID not found."""


class PineconeConflictError(PineconeError):
    """409 — duplicate name, index already exists, etc."""


class PineconeRateLimitError(PineconeError):
    """429 — Pinecone serverless rate limit (default 100 req/min) hit.

    `retry_after_s` mirrors the `Retry-After` header when Pinecone provides one.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_body: dict | None = None,
        retry_after_s: float = 5.0,
    ) -> None:
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s = retry_after_s


class PineconeServerError(PineconeError):
    """5xx — provider-side outage; retried with exponential backoff by the client."""


# ── Back-compat aliases ───────────────────────────────────────────────────────
# Older code paths may still import these names; keep them resolving so we
# never break a downstream caller.
PineconeNetworkError = PineconeServerError
PineconeNotFound = PineconeNotFoundError
