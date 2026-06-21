"""Pure utilities for the Iterable connector.

Keep this module free of HTTP, I/O, and connector-state coupling — it must
stay safe to import from any layer (connector.py, http_client.py, tests).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

T = TypeVar("T")


# ── Payload builders ────────────────────────────────────────────────────────


def build_user_identity_payload(
    email: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the canonical {email,userId} identity stanza Iterable expects.

    Raises:
        ValueError: If both email and user_id are None/empty.
    """
    if not email and not user_id:
        raise ValueError("Either email or user_id is required")
    payload: Dict[str, Any] = {}
    if email:
        payload["email"] = email
    if user_id:
        payload["userId"] = user_id
    return payload


def build_event_payload(
    email: str,
    event_name: str,
    data_fields: Optional[Dict[str, Any]] = None,
    campaign_id: Optional[int] = None,
    template_id: Optional[int] = None,
    user_id: Optional[str] = None,
    event_id: Optional[str] = None,
    created_at: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the body for POST /events/track."""
    if not email and not user_id:
        raise ValueError("Either email or user_id is required for track_event")
    if not event_name:
        raise ValueError("event_name is required")
    body: Dict[str, Any] = {"eventName": event_name}
    if email:
        body["email"] = email
    if user_id:
        body["userId"] = user_id
    if data_fields:
        body["dataFields"] = data_fields
    if campaign_id is not None:
        body["campaignId"] = int(campaign_id)
    if template_id is not None:
        body["templateId"] = int(template_id)
    if event_id is not None:
        body["id"] = event_id
    if created_at is not None:
        body["createdAt"] = int(created_at)
    return body


def normalize_subscribers(subscribers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validate and pass through a list of subscriber identity dicts.

    Iterable accepts each subscriber as `{email}`, `{userId}`, or both. This
    helper enforces that at least one identifier is present per record.

    Raises:
        ValueError: If `subscribers` is not a list, is empty, or any entry
            is missing both `email` and `userId`.
    """
    if not isinstance(subscribers, list):
        raise ValueError("subscribers must be a list")
    if not subscribers:
        raise ValueError("subscribers must be a non-empty list")
    normalized: List[Dict[str, Any]] = []
    for s in subscribers:
        if not isinstance(s, dict):
            raise ValueError("each subscriber must be a dict")
        if not s.get("email") and not s.get("userId"):
            raise ValueError("each subscriber must contain email or userId")
        normalized.append(s)
    return normalized


# ── Response parsers ────────────────────────────────────────────────────────


def parse_user_export(raw: Any) -> List[str]:
    """Parse the newline-delimited body of `GET /lists/getUsers` into emails.

    The endpoint streams `text/plain`, one email per line. Some workspaces
    return a JSON envelope `{"emails": [...]}`; this helper handles both.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, dict):
        emails = raw.get("emails") or raw.get("users") or []
        if isinstance(emails, list):
            return [str(x).strip() for x in emails if str(x).strip()]
        return []
    if isinstance(raw, (str, bytes)):
        text = raw.decode() if isinstance(raw, bytes) else raw
        return [line.strip() for line in text.splitlines() if line.strip()]
    return []


def ms_to_dt(value: Any) -> Optional[datetime]:
    """Convert an Iterable epoch-millis timestamp into a UTC datetime.

    Returns None on parse failure rather than raising — this is used by the
    normalizer where a missing/garbage timestamp should not abort ingestion.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# ── Async retry helper (used by sync()'s outer loop) ────────────────────────


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx; this helper is for the rare
    transient errors that escape the client (e.g. JSON decode flakiness on
    a flaky proxy), called from `sync()`'s outer iteration loop.
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
        raise last_exc  # pragma: no cover — defensive
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
