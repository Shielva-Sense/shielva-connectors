"""Misc utility helpers for the Bitrix24 connector."""
import asyncio
from typing import Any, Awaitable, Callable, Optional, TypeVar
from urllib.parse import urlparse

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


def extract_portal(webhook_url: str) -> str:
    """Derive the portal subdomain from a Bitrix24 webhook URL.

    >>> extract_portal("https://mycompany.bitrix24.com/rest/1/abc123xyz/")
    'mycompany'
    """
    if not webhook_url:
        return ""
    host = urlparse(webhook_url).hostname or ""
    if not host:
        return ""
    parts = host.split(".")
    return parts[0] if parts else ""


def normalize_phone_list(phones: Optional[Any]) -> Optional[list]:
    """Bitrix24 expects PHONE as `[{'VALUE': '...', 'VALUE_TYPE': 'WORK'}]`.

    Accepts a list of strings or pre-shaped dicts and normalizes to the
    canonical shape. Returns `None` for empty input.
    """
    if not phones:
        return None
    out = []
    for entry in phones:
        if isinstance(entry, dict):
            out.append(entry)
        else:
            out.append({"VALUE": str(entry), "VALUE_TYPE": "WORK"})
    return out


def normalize_email_list(emails: Optional[Any]) -> Optional[list]:
    """Mirror of `normalize_phone_list` for the EMAIL field."""
    if not emails:
        return None
    out = []
    for entry in emails:
        if isinstance(entry, dict):
            out.append(entry)
        else:
            out.append({"VALUE": str(entry), "VALUE_TYPE": "WORK"})
    return out
