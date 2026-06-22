from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import AirtableAuthError, AirtableError, AirtableRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 2.0  # Airtable rate limits aggressively (5 req/sec)
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
    Airtable rate limits aggressively (5 req/sec), so base_delay defaults to 2.0s.
    """
    last_exc: AirtableError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except AirtableAuthError:
            raise
        except AirtableRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except AirtableError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _stable_id(prefix: str, *parts: str) -> str:
    """Return a 16-character hex digest for a stable source_id.

    The key is SHA-256(prefix + ':' + ':'.join(parts))[:16].
    """
    raw = prefix + ":" + ":".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_record(
    record: dict[str, Any],
    base_id: str,
    base_name: str,
    table_name: str,
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw Airtable record into a ConnectorDocument.

    source_id = SHA-256("record:" + record_id)[:16] — deterministic, collision-resistant.
    """
    record_id: str = record.get("id", "")
    fields: dict[str, Any] = record.get("fields", {})
    created_time: str = record.get("createdTime", "")

    # Build a human-readable title from the first non-empty string field
    title_value: str = ""
    for val in fields.values():
        if isinstance(val, str) and val.strip():
            title_value = val.strip()
            break

    title = title_value or f"Record {record_id}"
    full_title = f"{base_name} / {table_name}: {title}"

    # Serialize all fields as content
    content_lines: list[str] = [f"Base: {base_name}", f"Table: {table_name}"]
    for key, val in fields.items():
        if isinstance(val, (list, dict)):
            content_lines.append(f"{key}: {json.dumps(val, ensure_ascii=False)}")
        else:
            content_lines.append(f"{key}: {val}")
    content = "\n".join(content_lines)

    source_id = _stable_id("record", record_id)
    source_url = f"https://airtable.com/{base_id}/{table_name}/{record_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=full_title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "record_id": record_id,
            "base_id": base_id,
            "base_name": base_name,
            "table_name": table_name,
            "fields": fields,
            "created_time": created_time,
        },
    )


def normalize_table(
    table: dict[str, Any],
    base_id: str,
) -> ConnectorDocument:
    """Convert an Airtable table schema object into a ConnectorDocument.

    source_id = SHA-256("table:" + table_id)[:16] — deterministic, collision-resistant.
    """
    table_id: str = table.get("id", "")
    table_name: str = table.get("name", table_id)
    primary_field_id: str = table.get("primaryFieldId", "")
    fields: list[dict[str, Any]] = table.get("fields", [])
    views: list[dict[str, Any]] = table.get("views", [])

    field_lines: list[str] = [
        f"- {f.get('name', '')} ({f.get('type', 'unknown')})" for f in fields
    ]
    view_lines: list[str] = [
        f"- {v.get('name', '')} ({v.get('type', 'unknown')})" for v in views
    ]

    content_parts: list[str] = [
        f"Table: {table_name}",
        f"Base ID: {base_id}",
        f"Table ID: {table_id}",
        f"Primary Field ID: {primary_field_id}",
        f"Fields ({len(fields)}):",
        *field_lines,
    ]
    if views:
        content_parts += [f"Views ({len(views)}):", *view_lines]

    source_id = _stable_id("table", table_id)

    return ConnectorDocument(
        source_id=source_id,
        title=f"Table Schema: {table_name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"https://airtable.com/{base_id}",
        metadata={
            "table_id": table_id,
            "table_name": table_name,
            "base_id": base_id,
            "primary_field_id": primary_field_id,
            "fields": fields,
            "views": views,
        },
    )
