"""Anthropic connector exception hierarchy.

Hierarchy:
    AnthropicError                       — base; carries status_code + response_body
    ├── AnthropicAuthError               — 401 / 403
    ├── AnthropicBadRequestError         — 400 / 413
    ├── AnthropicNotFoundError           — 404
    ├── AnthropicRateLimitError          — 429 (carries retry_after_s)
    ├── AnthropicServerError             — 5xx / 529 overloaded_error
    └── AnthropicNetworkError            — transport-layer (DNS / TCP / TLS / timeout)
"""


class AnthropicError(Exception):
    """Base for all Anthropic-connector errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class AnthropicAuthError(AnthropicError):
    """401 / 403 — api key invalid, revoked, or org lacks permission."""


class AnthropicBadRequestError(AnthropicError):
    """400 / 413 — malformed request body or payload too large."""


class AnthropicNotFoundError(AnthropicError):
    """404 — model id, batch id, or file id does not exist."""


class AnthropicRateLimitError(AnthropicError):
    """429 — rate limited. ``retry_after_s`` is the wait the API suggested."""

    def __init__(self, message: str, retry_after_s: float = 5.0) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class AnthropicServerError(AnthropicError):
    """5xx / 529 overloaded_error — provider-side outage; retry candidate."""


class AnthropicNetworkError(AnthropicError):
    """Transport-layer failure (DNS, TCP reset, TLS, timeout)."""


# Back-compat alias (older code imported this name).
AnthropicNotFound = AnthropicNotFoundError
