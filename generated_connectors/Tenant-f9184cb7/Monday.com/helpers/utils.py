"""Monday.com connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, List, Optional

from models import ConnectorDocument


def normalize_item(
    item: Dict[str, Any],
    board_id: str,
    board_name: str,
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Monday.com item object into a ConnectorDocument.

    The stable document id is sha256("item:" + item_id)[:16].
    """
    item_id = str(item.get("id", ""))
    item_name = item.get("name", "") or ""

    stable_id = hashlib.sha256(
        f"item:{item_id}".encode()
    ).hexdigest()[:16]

    # Human-readable title
    title = f"{item_name} [{board_name}]" if board_name else item_name or f"Monday item {item_id}"

    # Build content from column values
    column_values: List[Dict[str, Any]] = item.get("column_values") or []
    content_parts: list[str] = []
    if board_name:
        content_parts.append(f"Board: {board_name}")
    content_parts.append(f"Item: {item_name}")
    content_parts.append(f"Item ID: {item_id}")

    for col in column_values:
        col_id = col.get("id", "")
        text = col.get("text", "") or ""
        if text:
            content_parts.append(f"{col_id}: {text}")

    metadata: Dict[str, Any] = {
        "item_id": item_id,
        "item_name": item_name,
        "board_id": board_id,
        "board_name": board_name,
        "column_values": column_values,
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "source": "monday",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="monday_item",
        metadata=metadata,
    )


def normalize_board(
    board: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Monday.com board object into a ConnectorDocument.

    The stable document id is sha256("board:" + board_id)[:16].
    """
    board_id = str(board.get("id", ""))
    board_name = board.get("name", "") or ""
    description = board.get("description", "") or ""
    state = board.get("state", "") or ""

    stable_id = hashlib.sha256(
        f"board:{board_id}".encode()
    ).hexdigest()[:16]

    title = f"Board: {board_name}" if board_name else f"Monday board {board_id}"

    content_parts: list[str] = [
        f"Board: {board_name}",
        f"Board ID: {board_id}",
    ]
    if state:
        content_parts.append(f"State: {state}")
    if description:
        content_parts.append(f"Description: {description}")

    metadata: Dict[str, Any] = {
        "board_id": board_id,
        "board_name": board_name,
        "description": description,
        "state": state,
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "source": "monday",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="monday_board",
        metadata=metadata,
    )


async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on MondayAuthError — re-installing with a valid token is required.
    """
    from exceptions import MondayAuthError, MondayError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except MondayAuthError:
            raise
        except MondayError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
