"""Misc utility helpers for the YouTrack connector."""
import asyncio
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")


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


def normalize_base_url(base_url: str) -> str:
    """Return the YouTrack API base URL with a trailing ``/api`` segment.

    Accepts:
      - ``https://yourorg.youtrack.cloud``       → ``https://yourorg.youtrack.cloud/api``
      - ``https://yourorg.youtrack.cloud/``      → ``https://yourorg.youtrack.cloud/api``
      - ``https://yourorg.youtrack.cloud/api``   → ``https://yourorg.youtrack.cloud/api``
      - ``https://yourorg.youtrack.cloud/api/``  → ``https://yourorg.youtrack.cloud/api``
    """
    if not base_url:
        return ""
    url = base_url.strip().rstrip("/")
    if url.endswith("/api"):
        return url
    return f"{url}/api"


def issue_web_url(base_url: str, id_readable: str) -> Optional[str]:
    """Return the human-readable browser URL for an issue, or None if unknown."""
    if not id_readable:
        return None
    base = (base_url or "").rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    if not base:
        return None
    return f"{base}/issue/{id_readable}"


def extract_field_value(custom_fields: Optional[list], field_name: str) -> Any:
    """Look up ``field_name`` in a YouTrack ``customFields`` array.

    Returns ``None`` when absent. Shape::

        [{"name": "Priority", "value": {"name": "Major"}}, ...]
    """
    if not custom_fields:
        return None
    for entry in custom_fields:
        if not isinstance(entry, dict):
            continue
        if entry.get("name") != field_name:
            continue
        value = entry.get("value")
        if isinstance(value, dict):
            return value.get("name") or value.get("login") or value.get("id")
        return value
    return None


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
