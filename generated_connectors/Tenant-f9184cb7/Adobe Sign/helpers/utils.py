"""Misc utility helpers for the Adobe Sign connector.

Pure-stdlib helpers — no httpx, no normalization. Includes:

- ``with_retry`` — async exponential-backoff wrapper for unexpected transient errors.
- ``api_base_url_from_access_point`` — turns Adobe's discovered ``apiAccessPoint``
  (e.g. ``https://api.eu1.adobesign.com/``) into the v6 API root used by every call.
- ``build_oauth_authorize_url`` — Adobe Sign authorize URL builder.
- ``safe_get`` — nested-dict safe accessor.
"""
import asyncio
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import urlencode

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


def api_base_url_from_access_point(access_point: str) -> str:
    """Turn an Adobe ``apiAccessPoint`` value into the v6 API root.

    Adobe Sign's ``/baseUris`` endpoint returns something like::

        {"apiAccessPoint": "https://api.eu1.adobesign.com/", ...}

    The actual REST v6 endpoints live under ``{apiAccessPoint}api/rest/v6``.
    This helper normalises the trailing slash so callers can safely do
    ``f"{base}/agreements"`` without worrying about double slashes.
    """
    if not access_point:
        return ""
    trimmed = access_point.rstrip("/")
    if trimmed.endswith("/api/rest/v6"):
        return trimmed
    return f"{trimmed}/api/rest/v6"


def build_oauth_authorize_url(
    oauth_host: str,
    client_id: str,
    redirect_uri: str,
    scopes: str,
    state: str,
) -> str:
    """Build the Adobe Sign OAuth authorize URL.

    Adobe's authorize URL is ``{oauth_host}/public/oauth/v2`` with query params
    ``response_type=code``, ``client_id``, ``scope``, ``redirect_uri``, ``state``.
    """
    host = (oauth_host or "").rstrip("/")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
    }
    return f"{host}/public/oauth/v2?{urlencode(params)}"
