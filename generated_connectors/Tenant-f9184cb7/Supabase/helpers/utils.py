"""Shared utilities — PostgREST filter translation + retry helper."""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

import httpx
import structlog

from exceptions import SupabaseError, SupabaseServerError

logger = structlog.get_logger(__name__)

T = TypeVar("T")

RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 8.0
RATE_LIMIT_FIXED_DELAY_S: float = 5.0


# ── PostgREST filter helpers ──────────────────────────────────────────────

_POSTGREST_OPERATORS = frozenset({
    "eq", "neq", "gt", "gte", "lt", "lte",
    "like", "ilike", "is", "in", "cs", "cd",
    "sl", "sr", "nxr", "nxl", "adj", "ov",
    "fts", "plfts", "phfts", "wfts", "not",
})


def _format_filter_value(value: Any) -> str:
    """Translate a Python value to PostgREST filter syntax.

    Rules:
      - dict with a single operator key like {"gt": 5} → "gt.5"
      - list / tuple → "in.(v1,v2,v3)"
      - bool → "eq.true" / "eq.false" (PostgREST is case-sensitive on booleans)
      - everything else → "eq.<value>"
    """
    if isinstance(value, dict) and len(value) == 1:
        op, op_val = next(iter(value.items()))
        if op not in _POSTGREST_OPERATORS:
            # unknown operator — pass through as "<op>.<val>" anyway so callers
            # can use new PostgREST operators without bumping the connector
            return f"{op}.{op_val}"
        if isinstance(op_val, (list, tuple)):
            joined = ",".join(str(v) for v in op_val)
            return f"{op}.({joined})"
        return f"{op}.{op_val}"
    if isinstance(value, (list, tuple)):
        joined = ",".join(str(v) for v in value)
        return f"in.({joined})"
    if isinstance(value, bool):
        return f"eq.{'true' if value else 'false'}"
    return f"eq.{value}"


def build_postgrest_params(
    columns: str = "*",
    filter: Optional[Dict[str, Any]] = None,
    order: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a query-param dict suitable for the PostgREST REST API."""
    params: Dict[str, Any] = {"select": columns}
    if filter:
        for col, val in filter.items():
            params[col] = _format_filter_value(val)
    if order is not None:
        params["order"] = order
    if limit is not None:
        params["limit"] = str(limit)
    if offset is not None:
        params["offset"] = str(offset)
    return params


def build_filter_params(filter: Dict[str, Any]) -> Dict[str, Any]:
    """Build PostgREST filter-only params (no select)."""
    return {col: _format_filter_value(val) for col, val in (filter or {}).items()}


# ── Retry helper ──────────────────────────────────────────────────────────


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> T:
    """Run an async callable with exponential-backoff retry.

    Retries on:
      - ``SupabaseServerError`` (5xx)
      - ``SupabaseError`` with ``status_code == 429`` (uses fixed delay)
      - ``httpx.TransportError`` (network)

    Anything else (auth errors, not-found, conflict, bad-request) raises
    immediately — those are not transient.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except SupabaseError as exc:
            status = getattr(exc, "status_code", 0) or 0
            if status != 429 and not (500 <= status < 600):
                raise
            last_exc = exc
            if attempt == max_retries:
                break
            delay = RATE_LIMIT_FIXED_DELAY_S if status == 429 else min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "supabase.retry",
                attempt=attempt + 1,
                delay=delay,
                status=status,
            )
            await asyncio.sleep(delay)
        except httpx.TransportError as exc:
            last_exc = SupabaseServerError(f"Network error: {exc}")
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "supabase.transport_error",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise SupabaseServerError("with_retry: exhausted retries without exception")


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
