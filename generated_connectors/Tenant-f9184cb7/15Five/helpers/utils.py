from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import FifteenFiveAuthError, FifteenFiveError, FifteenFiveRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: FifteenFiveError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except FifteenFiveAuthError:
            raise
        except FifteenFiveRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = (
                exc.retry_after
                if exc.retry_after > 0
                else min(
                    base_delay * (RETRY_BACKOFF_FACTOR**attempt)
                    + random.uniform(0, RETRY_JITTER_S),
                    max_delay,
                )
            )
            await asyncio.sleep(delay)
        except FifteenFiveError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR**attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _short_hash(value: str) -> str:
    """Return a 16-character hex digest of SHA-256 for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def normalize_report(r: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw 15Five report/check-in into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of "report:{id}" so it is
    deterministic and collision-resistant.
    """
    report_id: str = str(r.get("id", ""))
    created_at: str = str(r.get("created_at", "") or r.get("date_completed", "") or "")
    responder: str = str(r.get("responder", "") or r.get("responder_name", "") or "")
    # responder may be a nested object or a plain string
    if isinstance(r.get("responder"), dict):
        resp_obj: dict[str, Any] = r["responder"]
        responder = (
            resp_obj.get("name", "")
            or f"{resp_obj.get('first_name', '')} {resp_obj.get('last_name', '')}".strip()
            or str(resp_obj.get("id", ""))
        )

    is_complete: bool = bool(r.get("is_complete", False))
    high_fives: int = int(r.get("high_fives_count", 0) or 0)

    content_parts: list[str] = [f"Report ID: {report_id}"]
    if responder:
        content_parts.append(f"Responder: {responder}")
    if created_at:
        content_parts.append(f"Date: {created_at}")
    content_parts.append(f"Complete: {is_complete}")
    if high_fives:
        content_parts.append(f"High Fives: {high_fives}")

    source_id = _short_hash(f"report:{report_id}")
    title = f"15Five Check-in #{report_id}"
    if responder:
        title = f"15Five Check-in by {responder} (#{report_id})"
    source_url = f"https://my.15five.com/report/{report_id}/"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "report_id": report_id,
            "responder": responder,
            "created_at": created_at,
            "is_complete": is_complete,
            "type": "checkin",
        },
    )


def normalize_objective(o: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw 15Five objective/OKR into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of "objective:{id}".
    """
    obj_id: str = str(o.get("id", ""))
    name: str = str(o.get("name", "") or o.get("title", "") or f"Objective {obj_id}")
    description: str = str(o.get("description", "") or "")
    owner: str = str(o.get("owner", "") or "")
    if isinstance(o.get("owner"), dict):
        owner_obj: dict[str, Any] = o["owner"]
        owner = (
            owner_obj.get("name", "")
            or f"{owner_obj.get('first_name', '')} {owner_obj.get('last_name', '')}".strip()
            or str(owner_obj.get("id", ""))
        )
    progress: float = float(o.get("progress", 0) or 0)
    status: str = str(o.get("status", "") or "")
    due_date: str = str(o.get("due_date", "") or "")
    start_date: str = str(o.get("start_date", "") or "")

    content_parts: list[str] = [f"Objective: {name}"]
    if owner:
        content_parts.append(f"Owner: {owner}")
    if description:
        content_parts.append(f"Description: {description}")
    if status:
        content_parts.append(f"Status: {status}")
    content_parts.append(f"Progress: {progress}%")
    if start_date:
        content_parts.append(f"Start: {start_date}")
    if due_date:
        content_parts.append(f"Due: {due_date}")

    source_id = _short_hash(f"objective:{obj_id}")
    title = f"OKR: {name}"
    source_url = f"https://my.15five.com/objective/{obj_id}/"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "objective_id": obj_id,
            "name": name,
            "owner": owner,
            "status": status,
            "progress": progress,
            "due_date": due_date,
            "start_date": start_date,
            "type": "objective",
        },
    )


def normalize_high_five(h: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw 15Five high five/recognition into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of "highfive:{id}".
    """
    hf_id: str = str(h.get("id", ""))
    message: str = str(h.get("message", "") or h.get("body", "") or "")
    created_at: str = str(h.get("created_at", "") or h.get("date", "") or "")

    # sender may be an object or string
    sender: str = str(h.get("sender", "") or h.get("from", "") or "")
    if isinstance(h.get("sender"), dict):
        s_obj: dict[str, Any] = h["sender"]
        sender = (
            s_obj.get("name", "")
            or f"{s_obj.get('first_name', '')} {s_obj.get('last_name', '')}".strip()
            or str(s_obj.get("id", ""))
        )

    # receivers may be a list of objects or strings
    raw_receivers: Any = h.get("receivers", h.get("to", []))
    receivers: list[str] = []
    if isinstance(raw_receivers, list):
        for rec in raw_receivers:
            if isinstance(rec, dict):
                name = (
                    rec.get("name", "")
                    or f"{rec.get('first_name', '')} {rec.get('last_name', '')}".strip()
                    or str(rec.get("id", ""))
                )
                receivers.append(name)
            elif rec:
                receivers.append(str(rec))
    elif raw_receivers:
        receivers.append(str(raw_receivers))

    receivers_str: str = ", ".join(receivers)

    content_parts: list[str] = [f"High Five #{hf_id}"]
    if sender:
        content_parts.append(f"From: {sender}")
    if receivers_str:
        content_parts.append(f"To: {receivers_str}")
    if message:
        content_parts.append(f"Message: {message}")
    if created_at:
        content_parts.append(f"Date: {created_at}")

    source_id = _short_hash(f"highfive:{hf_id}")
    title = f"High Five from {sender}" if sender else f"High Five #{hf_id}"
    if receivers_str:
        title += f" to {receivers_str}"
    source_url = f"https://my.15five.com/highfive/{hf_id}/"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "highfive_id": hf_id,
            "sender": sender,
            "receivers": receivers,
            "message": message,
            "created_at": created_at,
            "type": "recognition",
        },
    )
