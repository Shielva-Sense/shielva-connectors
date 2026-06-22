from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import SnowflakeAuthError, SnowflakeError, SnowflakeRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(entity_type: str, name: str) -> str:
    """SHA-256(entity_type + ':' + name)[:16] — stable, collision-resistant source ID."""
    raw = f"{entity_type}:{name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """
    Retry an async callable with exponential backoff + jitter.

    - SnowflakeAuthError is NOT retried — requires human intervention.
    - SnowflakeRateLimitError honours the retry_after value when > 0.
    - All other SnowflakeError subclasses are retried up to max_attempts times.
    """
    last_exc: SnowflakeError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except SnowflakeAuthError:
            raise  # auth errors are not retryable
        except SnowflakeRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except SnowflakeError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── Normalizers ────────────────────────────────────────────────────────────────


def normalize_database(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Snowflake database object into a ConnectorDocument.

    Stable id = sha256("database:" + name)[:16]
    """
    name: str = raw.get("name", "") or raw.get("NAME", "") or ""
    created_on: str = raw.get("created_on", "") or raw.get("CREATED_ON", "") or raw.get("createdOn", "") or ""
    owner: str = raw.get("owner", "") or raw.get("OWNER", "") or ""
    comment: str = raw.get("comment", "") or raw.get("COMMENT", "") or ""
    retention_time: int = int(raw.get("retention_time", raw.get("RETENTION_TIME", 1)) or 1)
    options: str = raw.get("options", "") or raw.get("OPTIONS", "") or ""

    source_id = _stable_id("database", name)
    title = f"Snowflake database: {name}"
    content_parts = [
        f"Database: {name}",
        f"Owner: {owner}" if owner else "",
        f"Comment: {comment}" if comment else "",
        f"Created: {created_on}" if created_on else "",
        f"Retention (days): {retention_time}",
        f"Options: {options}" if options else "",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "database",
            "name": name,
            "owner": owner,
            "comment": comment,
            "created_on": created_on,
            "retention_time": retention_time,
            "options": options,
        },
    )


def normalize_schema(
    raw: dict[str, Any],
    database: str = "",
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Snowflake schema object into a ConnectorDocument.

    Stable id = sha256("schema:" + database + "." + name)[:16]
    """
    name: str = raw.get("name", "") or raw.get("NAME", "") or ""
    db_name: str = raw.get("database_name", "") or raw.get("DATABASE_NAME", "") or database or ""
    created_on: str = raw.get("created_on", "") or raw.get("CREATED_ON", "") or raw.get("createdOn", "") or ""
    owner: str = raw.get("owner", "") or raw.get("OWNER", "") or ""
    comment: str = raw.get("comment", "") or raw.get("COMMENT", "") or ""
    retention_time: int = int(raw.get("retention_time", raw.get("RETENTION_TIME", 1)) or 1)
    options: str = raw.get("options", "") or raw.get("OPTIONS", "") or ""

    qualified_name = f"{db_name}.{name}" if db_name else name
    source_id = _stable_id("schema", qualified_name)
    title = f"Snowflake schema: {qualified_name}"
    content_parts = [
        f"Schema: {qualified_name}",
        f"Database: {db_name}" if db_name else "",
        f"Owner: {owner}" if owner else "",
        f"Comment: {comment}" if comment else "",
        f"Created: {created_on}" if created_on else "",
        f"Retention (days): {retention_time}",
        f"Options: {options}" if options else "",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "schema",
            "name": name,
            "database_name": db_name,
            "qualified_name": qualified_name,
            "owner": owner,
            "comment": comment,
            "created_on": created_on,
            "retention_time": retention_time,
            "options": options,
        },
    )


def normalize_table(
    raw: dict[str, Any],
    database: str = "",
    schema: str = "",
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Snowflake table object into a ConnectorDocument.

    Stable id = sha256("table:" + database + "." + schema + "." + name)[:16]
    """
    name: str = raw.get("name", "") or raw.get("NAME", "") or ""
    db_name: str = raw.get("database_name", "") or raw.get("DATABASE_NAME", "") or database or ""
    schema_name: str = raw.get("schema_name", "") or raw.get("SCHEMA_NAME", "") or schema or ""
    kind: str = raw.get("kind", "") or raw.get("KIND", "") or "TABLE"
    created_on: str = raw.get("created_on", "") or raw.get("CREATED_ON", "") or raw.get("createdOn", "") or ""
    owner: str = raw.get("owner", "") or raw.get("OWNER", "") or ""
    comment: str = raw.get("comment", "") or raw.get("COMMENT", "") or ""
    rows: int = int(raw.get("rows", raw.get("ROWS", raw.get("num_rows", 0))) or 0)
    bytes_size: int = int(raw.get("bytes", raw.get("BYTES", 0)) or 0)
    cluster_by: str = raw.get("cluster_by", "") or raw.get("CLUSTER_BY", "") or ""

    qualified_name = f"{db_name}.{schema_name}.{name}" if db_name and schema_name else name
    source_id = _stable_id("table", qualified_name)
    title = f"Snowflake table: {qualified_name}"
    content_parts = [
        f"Table: {qualified_name}",
        f"Database: {db_name}" if db_name else "",
        f"Schema: {schema_name}" if schema_name else "",
        f"Kind: {kind}",
        f"Owner: {owner}" if owner else "",
        f"Rows: {rows}",
        f"Size (bytes): {bytes_size}",
        f"Cluster by: {cluster_by}" if cluster_by else "",
        f"Comment: {comment}" if comment else "",
        f"Created: {created_on}" if created_on else "",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "table",
            "name": name,
            "database_name": db_name,
            "schema_name": schema_name,
            "qualified_name": qualified_name,
            "kind": kind,
            "owner": owner,
            "comment": comment,
            "created_on": created_on,
            "rows": rows,
            "bytes_size": bytes_size,
            "cluster_by": cluster_by,
        },
    )
