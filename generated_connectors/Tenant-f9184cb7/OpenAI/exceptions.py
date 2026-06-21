"""Custom exception hierarchy for the OpenAI connector."""

from __future__ import annotations

from typing import Any, Dict, Optional


class OpenAIError(Exception):
    """Base for all OpenAI-connector errors.

    Carries the HTTP `status_code` (0 for non-HTTP errors) and the raw
    `response_body` parsed from the OpenAI error envelope where available.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message: str = message
        self.status_code: int = status_code
        self.response_body: Dict[str, Any] = response_body or {}


class OpenAIAuthError(OpenAIError):
    """401 / 403 from OpenAI — invalid or revoked key, or org mismatch."""


class OpenAIBadRequestError(OpenAIError):
    """400 — malformed payload (bad model id, missing required field)."""


class OpenAINotFoundError(OpenAIError):
    """404 — model / file / resource not found."""


class OpenAIConflictError(OpenAIError):
    """409 — duplicate resource."""


class OpenAIRateLimitError(OpenAIError):
    """429 — rate limited. `retry_after_s` carries the server hint."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_body: Optional[Dict[str, Any]] = None,
        retry_after_s: float = 1.0,
    ) -> None:
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.retry_after_s: float = retry_after_s


class OpenAIServerError(OpenAIError):
    """5xx — provider-side outage; retry candidate."""


class OpenAINetworkError(OpenAIError):
    """Transport-layer failure before a response was received (DNS, timeout, TLS)."""
