"""Misc utility helpers for the OneSignal connector."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc  # pragma: no cover
    raise RuntimeError("with_retry: exhausted retries without exception")


def prune_none(d: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of ``d`` with ``None`` values removed.

    OneSignal endpoints reject some ``null`` fields outright — pruning them
    before serialization keeps the wire payload minimal and predictable.
    """
    return {k: v for k, v in d.items() if v is not None}


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


def parse_dt(value: Any) -> datetime:
    """Parse an ISO 8601 string or unix timestamp into a tz-aware datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def build_notification_payload(
    app_id: str,
    contents: Dict[str, Any],
    *,
    headings: Optional[Dict[str, Any]] = None,
    included_segments: Optional[Iterable[str]] = None,
    excluded_segments: Optional[Iterable[str]] = None,
    include_player_ids: Optional[Iterable[str]] = None,
    include_external_user_ids: Optional[Iterable[str]] = None,
    data: Optional[Dict[str, Any]] = None,
    url: Optional[str] = None,
    big_picture: Optional[str] = None,
    send_after: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the JSON payload for ``POST /notifications``.

    Mirrors the public surface of ``OneSignalConnector.send_notification``.
    Kept separate so the connector method stays a thin orchestrator and the
    payload shape is testable in isolation.
    """
    payload: Dict[str, Any] = {
        "app_id": app_id,
        "contents": contents,
    }
    if headings:
        payload["headings"] = headings
    if included_segments:
        payload["included_segments"] = list(included_segments)
    if excluded_segments:
        payload["excluded_segments"] = list(excluded_segments)
    if include_player_ids:
        payload["include_player_ids"] = list(include_player_ids)
    if include_external_user_ids:
        payload["include_external_user_ids"] = list(include_external_user_ids)
    if data:
        payload["data"] = data
    if url:
        payload["url"] = url
    if big_picture:
        payload["big_picture"] = big_picture
    if send_after:
        payload["send_after"] = send_after
    return payload
