"""Custom exceptions for the Nutshell connector.

Nutshell uses JSON-RPC 2.0 over HTTPS — errors arrive in two ways:

1. HTTP-level errors (401, 429, 5xx) — handled at the response status layer.
2. JSON-RPC envelope errors — the response is HTTP 200 with
   ``{"jsonrpc": "2.0", "id": N, "error": {"code": <int>, "message": "..."}}``.

The HTTP client parses both shapes and raises the typed exceptions below.
"""
from typing import Any, Dict, Optional


class NutshellError(Exception):
    """Base exception for all Nutshell connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        rpc_code: Optional[int] = None,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.rpc_code = rpc_code
        self.response_body = response_body or {}


class NutshellAuthError(NutshellError):
    """Raised when authentication fails — bad api_key/username or HTTP 401."""


class NutshellNetworkError(NutshellError):
    """Raised on transport-level failures or 5xx after retries are exhausted."""


class NutshellNotFound(NutshellError):
    """Raised when a referenced contact / lead / account does not exist."""


class NutshellRateLimitError(NutshellError):
    """Raised when the Nutshell API returns HTTP 429 after retries are exhausted."""
