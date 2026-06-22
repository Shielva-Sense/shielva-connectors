"""Coda connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Callable, Dict, List, Optional

from models import ConnectorDocument


# ── stable ID helpers ─────────────────────────────────────────────────────────

def _stable_id(prefix: str, raw_id: str) -> str:
    """Return a 16-char stable id: sha256("{prefix}:{raw_id}")[:16]."""
    key = f"{prefix}:{raw_id}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ── normalization functions ───────────────────────────────────────────────────

def normalize_doc(raw: Dict[str, Any]) -> ConnectorDocument:
    """Convert a Coda doc object into a ConnectorDocument.

    Stable id = sha256("doc:{id}")[:16].
    """
    doc_id = raw.get("id", "")
    stable = _stable_id("doc", doc_id)

    name = raw.get("name", "Untitled Doc")
    href = raw.get("href", "")
    browser_link = raw.get("browserLink", "")
    created_at = raw.get("createdAt", "")
    updated_at = raw.get("updatedAt", "")
    owner = raw.get("owner", "")
    owner_name = raw.get("ownerName", "")
    folder_id = raw.get("folder", {}).get("id", "") if isinstance(raw.get("folder"), dict) else ""

    content_parts: List[str] = [f"Doc: {name}"]
    if browser_link:
        content_parts.append(f"URL: {browser_link}")
    if owner_name:
        content_parts.append(f"Owner: {owner_name}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    metadata: Dict[str, Any] = {
        "doc_id": doc_id,
        "href": href,
        "browser_link": browser_link,
        "created_at": created_at,
        "updated_at": updated_at,
        "owner": owner,
        "owner_name": owner_name,
        "folder_id": folder_id,
        "resource_type": "doc",
        "source": "coda",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="coda_doc",
        metadata=metadata,
    )


def normalize_page(raw: Dict[str, Any], doc_id: str) -> ConnectorDocument:
    """Convert a Coda page object into a ConnectorDocument.

    Stable id = sha256("page:{id}")[:16].
    """
    page_id = raw.get("id", "")
    stable = _stable_id("page", page_id)

    name = raw.get("name", "Untitled Page")
    browser_link = raw.get("browserLink", "")
    href = raw.get("href", "")
    created_at = raw.get("createdAt", "")
    updated_at = raw.get("updatedAt", "")
    page_type = raw.get("type", "canvas")
    parent_page_id = raw.get("parent", {}).get("id", "") if isinstance(raw.get("parent"), dict) else ""

    content_parts: List[str] = [f"Page: {name}"]
    if browser_link:
        content_parts.append(f"URL: {browser_link}")
    if page_type:
        content_parts.append(f"Type: {page_type}")
    if parent_page_id:
        content_parts.append(f"Parent page: {parent_page_id}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    metadata: Dict[str, Any] = {
        "page_id": page_id,
        "doc_id": doc_id,
        "href": href,
        "browser_link": browser_link,
        "page_type": page_type,
        "parent_page_id": parent_page_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "resource_type": "page",
        "source": "coda",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="coda_page",
        metadata=metadata,
    )


def normalize_table(raw: Dict[str, Any], doc_id: str) -> ConnectorDocument:
    """Convert a Coda table object into a ConnectorDocument.

    Stable id = sha256("table:{id}")[:16].
    """
    table_id = raw.get("id", "")
    stable = _stable_id("table", table_id)

    name = raw.get("name", "Untitled Table")
    href = raw.get("href", "")
    browser_link = raw.get("browserLink", "")
    table_type = raw.get("tableType", "table")
    created_at = raw.get("createdAt", "")
    updated_at = raw.get("updatedAt", "")
    row_count = raw.get("rowCount", 0)

    # Column names from the columns array if present
    columns: List[str] = [
        col.get("name", "") for col in raw.get("columns", []) if isinstance(col, dict)
    ]

    content_parts: List[str] = [f"Table: {name}"]
    if browser_link:
        content_parts.append(f"URL: {browser_link}")
    if table_type:
        content_parts.append(f"Table type: {table_type}")
    if row_count:
        content_parts.append(f"Rows: {row_count}")
    if columns:
        content_parts.append(f"Columns: {', '.join(c for c in columns if c)}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    metadata: Dict[str, Any] = {
        "table_id": table_id,
        "doc_id": doc_id,
        "href": href,
        "browser_link": browser_link,
        "table_type": table_type,
        "row_count": row_count,
        "columns": columns,
        "created_at": created_at,
        "updated_at": updated_at,
        "resource_type": "table",
        "source": "coda",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="coda_table",
        metadata=metadata,
    )


def normalize_row(raw: Dict[str, Any], doc_id: str, table_id: str) -> ConnectorDocument:
    """Convert a Coda row object into a ConnectorDocument.

    Stable id = sha256("row:{id}")[:16].
    Content is the JSON-serialized cells dict.
    """
    row_id = raw.get("id", "")
    stable = _stable_id("row", row_id)

    name = raw.get("name", f"Row {row_id}")
    href = raw.get("href", "")
    browser_link = raw.get("browserLink", "")
    created_at = raw.get("createdAt", "")
    updated_at = raw.get("updatedAt", "")

    # Coda rows carry cell values under "values" dict: {column_id: value}
    values = raw.get("values", {})
    cells_json = json.dumps(values, default=str, ensure_ascii=False)

    content_parts: List[str] = [f"Row: {name}"]
    if browser_link:
        content_parts.append(f"URL: {browser_link}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")
    content_parts.append(f"Cells: {cells_json}")

    metadata: Dict[str, Any] = {
        "row_id": row_id,
        "doc_id": doc_id,
        "table_id": table_id,
        "href": href,
        "browser_link": browser_link,
        "created_at": created_at,
        "updated_at": updated_at,
        "values": values,
        "resource_type": "row",
        "source": "coda",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="coda_row",
        metadata=metadata,
    )


# ── retry utility ─────────────────────────────────────────────────────────────

async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on CodaAuthError — re-authorizing is required.
    Skips retry on CodaNotFoundError — the resource does not exist.
    """
    from exceptions import CodaAuthError, CodaError, CodaNotFoundError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except CodaAuthError:
            raise
        except CodaNotFoundError:
            raise
        except CodaError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
