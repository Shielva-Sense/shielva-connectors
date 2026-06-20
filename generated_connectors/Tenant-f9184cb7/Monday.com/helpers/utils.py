"""Monday.com connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, List, Optional

from models import ConnectorDocument


def normalize_board(board: Dict[str, Any]) -> ConnectorDocument:
    """Convert a Monday.com board object into a ConnectorDocument.

    Stable document id = sha256("board:" + str(board_id))[:16].
    """
    board_id = str(board.get("id", ""))
    board_name = board.get("name", "") or ""
    description = board.get("description", "") or ""
    state = board.get("state", "") or ""

    stable_id = hashlib.sha256(
        f"board:{board_id}".encode()
    ).hexdigest()[:16]

    title = f"Board: {board_name}" if board_name else f"Monday.com board {board_id}"

    content_parts: List[str] = [f"Board: {board_name}", f"Board ID: {board_id}"]
    if state:
        content_parts.append(f"State: {state}")
    if description:
        content_parts.append(f"Description: {description}")

    # Include groups if present
    groups: List[Dict[str, Any]] = board.get("groups") or []
    for group in groups:
        g_title = group.get("title", "") or ""
        if g_title:
            content_parts.append(f"Group: {g_title}")

    # Include column info if present
    columns: List[Dict[str, Any]] = board.get("columns") or []
    if columns:
        col_names = [col.get("title", "") or col.get("id", "") for col in columns]
        content_parts.append(f"Columns: {', '.join(col_names)}")

    metadata: Dict[str, Any] = {
        "board_id": board_id,
        "board_name": board_name,
        "description": description,
        "state": state,
        "groups": groups,
        "columns": columns,
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        source="monday_com",
        type="board",
        metadata=metadata,
    )


def normalize_item(item: Dict[str, Any], board_id: str) -> ConnectorDocument:
    """Convert a Monday.com item object into a ConnectorDocument.

    Stable document id = sha256("item:" + str(item_id))[:16].
    """
    item_id = str(item.get("id", ""))
    item_name = item.get("name", "") or ""

    stable_id = hashlib.sha256(
        f"item:{item_id}".encode()
    ).hexdigest()[:16]

    title = item_name or f"Monday.com item {item_id}"

    content_parts: List[str] = [
        f"Item: {item_name}",
        f"Item ID: {item_id}",
        f"Board ID: {board_id}",
    ]

    column_values: List[Dict[str, Any]] = item.get("column_values") or []
    for col in column_values:
        col_id = col.get("id", "")
        text = col.get("text", "") or ""
        if text:
            content_parts.append(f"{col_id}: {text}")

    metadata: Dict[str, Any] = {
        "item_id": item_id,
        "item_name": item_name,
        "board_id": board_id,
        "column_values": column_values,
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        source="monday_com",
        type="work_item",
        metadata=metadata,
    )


async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on MondayComAuthError — a credential fix is required.
    """
    from exceptions import MondayComAuthError, MondayComError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except MondayComAuthError:
            raise
        except (MondayComError, Exception) as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))

    raise last_exc  # type: ignore[misc]
