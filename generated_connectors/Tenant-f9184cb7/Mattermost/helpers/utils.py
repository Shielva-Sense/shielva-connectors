"""Shared utilities for the Mattermost connector."""
import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")
_API_SUFFIX = "/api/v4"


def normalize_server_url(server_url: str) -> str:
    """Return a clean ``https://host[:port]`` base with no trailing slash and no API suffix.

    Examples
    --------
    >>> normalize_server_url("https://mm.acme.com/")
    'https://mm.acme.com'
    >>> normalize_server_url("https://mm.acme.com/api/v4")
    'https://mm.acme.com'
    >>> normalize_server_url("mm.acme.com")
    'https://mm.acme.com'
    """
    url = (server_url or "").strip()
    if not url:
        return ""
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    url = url.rstrip("/")
    if url.endswith(_API_SUFFIX):
        url = url[: -len(_API_SUFFIX)]
    return url


def safe_int(value: object, default: int = 0) -> int:
    """Best-effort int coercion that never raises."""
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def extract_message(body: object) -> Optional[str]:
    """Pull the human-readable error message from a Mattermost error body."""
    if isinstance(body, dict):
        return (
            body.get("message")
            or body.get("detailed_error")
            or body.get("error")
        )
    return None


def ms_to_dt(ms: Any) -> datetime:
    """Convert a Mattermost ms-epoch int to a tz-aware datetime."""
    try:
        if ms in (None, 0, ""):
            return datetime.now(timezone.utc)
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return datetime.now(timezone.utc)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx; this helper retries unexpected
    transient errors that escape the client (e.g. JSON decode flakiness on
    intermittent proxies).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")
