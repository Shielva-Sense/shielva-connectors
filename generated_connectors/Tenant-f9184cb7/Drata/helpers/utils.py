"""Misc utility helpers for the Drata connector."""
import asyncio
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, TypeVar

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


def coerce_items(payload: Any) -> Iterable[Dict[str, Any]]:
    """Yield list items from a Drata list response.

    Drata returns either a top-level list, or `{"data": [...]}`, or
    `{"items": [...]}`, depending on the endpoint.
    """
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(payload, dict):
        for key in ("data", "items", "results"):
            collection = payload.get(key)
            if isinstance(collection, list):
                for item in collection:
                    if isinstance(item, dict):
                        yield item
                return


def build_list_params(
    limit: int = 100,
    offset: int = 0,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a Drata list-endpoint query-param dict (drops None values)."""
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if extra:
        for k, v in extra.items():
            if v is not None:
                params[k] = v
    return params
