"""Smartsheet connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Callable, Dict, List, Optional

from models import ConnectorDocument


def normalize_sheet(raw: Dict[str, Any]) -> ConnectorDocument:
    """Convert a Smartsheet sheet summary object into a ConnectorDocument.

    Stable document id: sha256("sheet:" + str(id))[:16]
    """
    sheet_id = str(raw.get("id", ""))
    name = raw.get("name", "") or ""
    permalink = raw.get("permalink", "") or ""
    access_level = raw.get("accessLevel", "") or ""
    created_at = raw.get("createdAt", "") or ""
    modified_at = raw.get("modifiedAt", "") or ""
    total_row_count = raw.get("totalRowCount", 0)

    stable_id = hashlib.sha256(f"sheet:{sheet_id}".encode()).hexdigest()[:16]

    title = f"Sheet: {name}" if name else f"Smartsheet sheet {sheet_id}"

    content_parts: List[str] = [
        f"Sheet: {name}",
        f"Sheet ID: {sheet_id}",
    ]
    if access_level:
        content_parts.append(f"Access Level: {access_level}")
    if total_row_count:
        content_parts.append(f"Total Rows: {total_row_count}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if modified_at:
        content_parts.append(f"Modified: {modified_at}")
    if permalink:
        content_parts.append(f"URL: {permalink}")

    metadata: Dict[str, Any] = {
        "sheet_id": sheet_id,
        "name": name,
        "permalink": permalink,
        "access_level": access_level,
        "created_at": created_at,
        "modified_at": modified_at,
        "total_row_count": total_row_count,
        "source": "smartsheet",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="smartsheet_sheet",
        metadata=metadata,
    )


def normalize_row(raw: Dict[str, Any], sheet_id: int) -> ConnectorDocument:
    """Convert a Smartsheet row object into a ConnectorDocument.

    Stable document id: sha256("row:" + str(row_id))[:16]
    Content: JSON representation of the row's cells.
    """
    row_id = str(raw.get("id", ""))
    row_number = raw.get("rowNumber", 0)
    cells: List[Dict[str, Any]] = raw.get("cells", []) or []
    created_at = raw.get("createdAt", "") or ""
    modified_at = raw.get("modifiedAt", "") or ""

    stable_id = hashlib.sha256(f"row:{row_id}".encode()).hexdigest()[:16]

    title = f"Row {row_number} (Sheet {sheet_id})"

    # Build a human-readable cell summary and JSON content
    cell_summaries: List[str] = []
    for cell in cells:
        col_id = cell.get("columnId", "")
        display_val = cell.get("displayValue", "") or cell.get("value", "")
        if display_val is not None and display_val != "":
            cell_summaries.append(f"Column {col_id}: {display_val}")

    content_parts: List[str] = [
        f"Row: {row_number}",
        f"Row ID: {row_id}",
        f"Sheet ID: {sheet_id}",
    ]
    if cell_summaries:
        content_parts.extend(cell_summaries)
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if modified_at:
        content_parts.append(f"Modified: {modified_at}")

    # Also store raw cells as JSON in content for full-fidelity search
    content_parts.append(f"Cells JSON: {json.dumps(cells)}")

    metadata: Dict[str, Any] = {
        "row_id": row_id,
        "row_number": row_number,
        "sheet_id": str(sheet_id),
        "cells": cells,
        "created_at": created_at,
        "modified_at": modified_at,
        "source": "smartsheet",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="smartsheet_row",
        metadata=metadata,
    )


def normalize_workspace(raw: Dict[str, Any]) -> ConnectorDocument:
    """Convert a Smartsheet workspace object into a ConnectorDocument.

    Stable document id: sha256("workspace:" + str(id))[:16]
    """
    workspace_id = str(raw.get("id", ""))
    name = raw.get("name", "") or ""
    access_level = raw.get("accessLevel", "") or ""

    stable_id = hashlib.sha256(f"workspace:{workspace_id}".encode()).hexdigest()[:16]

    title = f"Workspace: {name}" if name else f"Smartsheet workspace {workspace_id}"

    content_parts: List[str] = [
        f"Workspace: {name}",
        f"Workspace ID: {workspace_id}",
    ]
    if access_level:
        content_parts.append(f"Access Level: {access_level}")

    metadata: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "name": name,
        "access_level": access_level,
        "source": "smartsheet",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="smartsheet_workspace",
        metadata=metadata,
    )


def normalize_report(raw: Dict[str, Any]) -> ConnectorDocument:
    """Convert a Smartsheet report object into a ConnectorDocument.

    Stable document id: sha256("report:" + str(id))[:16]
    """
    report_id = str(raw.get("id", ""))
    name = raw.get("name", "") or ""
    access_level = raw.get("accessLevel", "") or ""
    created_at = raw.get("createdAt", "") or ""
    modified_at = raw.get("modifiedAt", "") or ""

    stable_id = hashlib.sha256(f"report:{report_id}".encode()).hexdigest()[:16]

    title = f"Report: {name}" if name else f"Smartsheet report {report_id}"

    content_parts: List[str] = [
        f"Report: {name}",
        f"Report ID: {report_id}",
    ]
    if access_level:
        content_parts.append(f"Access Level: {access_level}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if modified_at:
        content_parts.append(f"Modified: {modified_at}")

    metadata: Dict[str, Any] = {
        "report_id": report_id,
        "name": name,
        "access_level": access_level,
        "created_at": created_at,
        "modified_at": modified_at,
        "source": "smartsheet",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="smartsheet_report",
        metadata=metadata,
    )


async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on SmartsheetAuthError — re-installing with a valid token
    is required; retrying with the same bad token will always fail.
    """
    from exceptions import SmartsheetAuthError, SmartsheetError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except SmartsheetAuthError:
            raise
        except SmartsheetError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
