"""Microsoft Graph query helpers + small async utilities.

Single owner of:
- `$select` / `$top` / `$filter` / `$search` / `$orderby` query composition
- `directoryObjects` $ref body construction
- `with_retry` async wrapper for transient escape from the HTTP client
- `@odata.nextLink` page extraction helper
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, TypeVar

T = TypeVar("T")


def build_graph_query(
    *,
    top: Optional[int] = None,
    filter: Optional[str] = None,
    search: Optional[str] = None,
    orderby: Optional[str] = None,
    select: Optional[Iterable[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compose a Microsoft Graph ``$``-prefixed query-string dict.

    Empty/None values are dropped. ``select`` accepts an iterable of property
    names and is joined with commas. ``search`` is auto-double-quoted as Graph
    requires.
    """
    params: Dict[str, Any] = {}
    if top is not None:
        params["$top"] = int(top)
    if filter:
        params["$filter"] = filter
    if search:
        params["$search"] = search if search.startswith('"') else f'"{search}"'
    if orderby:
        params["$orderby"] = orderby
    if select:
        joined = ",".join(p.strip() for p in select if p and p.strip())
        if joined:
            params["$select"] = joined
    if extra:
        for k, v in extra.items():
            if v is not None and v != "":
                params[k] = v
    return params


def directory_object_ref(graph_base: str, object_id: str) -> Dict[str, str]:
    """Build the @odata.id body for /groups/{id}/members/$ref additions."""
    base = graph_base.rstrip("/")
    return {"@odata.id": f"{base}/directoryObjects/{object_id}"}


def collect_page_value(initial: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract the ``value`` array from a Graph collection response, defensively."""
    if not isinstance(initial, dict):
        return []
    value = initial.get("value")
    if isinstance(value, list):
        return value
    return []


def next_link(page: Dict[str, Any]) -> Optional[str]:
    """Return the next-page URL from a Graph collection response, if any."""
    if not isinstance(page, dict):
        return None
    nxt = page.get("@odata.nextLink")
    return nxt if isinstance(nxt, str) and nxt else None


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
