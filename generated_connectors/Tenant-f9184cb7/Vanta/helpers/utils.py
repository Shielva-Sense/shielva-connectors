"""Misc utility helpers for the Vanta connector.

Pure functions + retry wrapper — no I/O state, no module-level network.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

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
    last_exc: Optional[Exception] = None
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


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Best-effort coerce a value to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y"):
            return True
        if lowered in ("false", "0", "no", "n", ""):
            return False
    return default


def build_pagination_params(
    page_size: int = 50,
    page_cursor: Optional[str] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compose a query-param dict for Vanta paginated GETs.

    Vanta uses `pageSize` + `pageCursor` consistently across collection
    endpoints; this helper enforces that shape so callers cannot drift.
    """
    params: Dict[str, Any] = {"pageSize": int(page_size)}
    if page_cursor:
        params["pageCursor"] = page_cursor
    if extras:
        for key, value in extras.items():
            if value is None:
                continue
            params[key] = value
    return params
