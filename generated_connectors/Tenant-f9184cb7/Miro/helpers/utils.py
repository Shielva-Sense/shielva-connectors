"""Miro connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, List, Optional

from models import ConnectorDocument


# ── stable id helpers ──────────────────────────────────────────────────────────

def _stable_id(prefix: str, key: str) -> str:
    """Produce a 16-char hex stable ID: sha256('<prefix>:<key>')[:16]."""
    raw = f"{prefix}:{key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── normalizers ───────────────────────────────────────────────────────────────

def normalize_board(raw: Dict[str, Any]) -> ConnectorDocument:
    """Normalize a Miro board API response into a ConnectorDocument.

    Stable id = sha256('board:<id>')[:16].
    Content is assembled from all available board metadata fields.
    """
    board_id: str = raw.get("id", "")
    name: str = raw.get("name", "Untitled Board")
    description: str = raw.get("description", "")
    created_at: str = raw.get("createdAt", "")
    modified_at: str = raw.get("modifiedAt", "")
    view_link: str = raw.get("viewLink", "")

    # Sharing policy is nested under policy.sharingPolicy.access
    policy: Dict[str, Any] = raw.get("policy", {})
    sharing_policy: Dict[str, Any] = policy.get("sharingPolicy", {})
    access: str = sharing_policy.get("access", "")

    # Owner info
    owner: Dict[str, Any] = raw.get("owner", {})
    owner_id: str = owner.get("id", "")
    owner_name: str = owner.get("name", "")

    # Team info
    team: Dict[str, Any] = raw.get("team", {})
    team_id: str = team.get("id", "")
    team_name: str = team.get("name", "")

    stable = _stable_id("board", board_id)

    content_parts: List[str] = [f"Board: {name}"]
    if description:
        content_parts.append(f"Description: {description}")
    if board_id:
        content_parts.append(f"ID: {board_id}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if modified_at:
        content_parts.append(f"Modified: {modified_at}")
    if access:
        content_parts.append(f"Access policy: {access}")
    if owner_name:
        content_parts.append(f"Owner: {owner_name}")
    if team_name:
        content_parts.append(f"Team: {team_name}")
    if view_link:
        content_parts.append(f"Link: {view_link}")

    metadata: Dict[str, Any] = {
        "board_id": board_id,
        "name": name,
        "description": description,
        "created_at": created_at,
        "modified_at": modified_at,
        "view_link": view_link,
        "access_policy": access,
        "owner_id": owner_id,
        "owner_name": owner_name,
        "team_id": team_id,
        "team_name": team_name,
        "object_type": "board",
        "source": "miro",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="miro_board",
        metadata=metadata,
    )


def normalize_item(raw: Dict[str, Any], board_id: str = "") -> ConnectorDocument:
    """Normalize a Miro board item API response into a ConnectorDocument.

    Stable id = sha256('item:<id>')[:16].
    Content is extracted from data.content (sticky notes / text / cards)
    or data.title (cards), with positional context added.
    """
    item_id: str = str(raw.get("id", ""))
    item_type: str = raw.get("type", "item")
    created_at: str = raw.get("createdAt", "")
    modified_at: str = raw.get("modifiedAt", "")

    # Content: sticky notes and text items store content under data.content;
    # cards store their title under data.title.
    data: Dict[str, Any] = raw.get("data", {})
    content_text: str = data.get("content", "") or data.get("title", "")

    # Position info
    position: Dict[str, Any] = raw.get("position", {})
    pos_x: Optional[float] = position.get("x") if position else None
    pos_y: Optional[float] = position.get("y") if position else None

    # Creator / modifier (nested objects with id)
    created_by_obj: Dict[str, Any] = raw.get("createdBy", {})
    created_by: str = created_by_obj.get("id", "") if created_by_obj else ""
    modified_by_obj: Dict[str, Any] = raw.get("modifiedBy", {})
    modified_by: str = modified_by_obj.get("id", "") if modified_by_obj else ""

    # Style colour for sticky notes
    style: Dict[str, Any] = raw.get("style", {})
    fill_color: str = style.get("fillColor", "") if style else ""

    stable = _stable_id("item", item_id)

    title = content_text[:80] if content_text else f"{item_type.title()} {item_id}"

    content_parts: List[str] = [f"Item type: {item_type}"]
    if content_text:
        content_parts.append(f"Content: {content_text}")
    if board_id:
        content_parts.append(f"Board ID: {board_id}")
    if item_id:
        content_parts.append(f"Item ID: {item_id}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if modified_at:
        content_parts.append(f"Modified: {modified_at}")
    if pos_x is not None:
        content_parts.append(f"Position: x={pos_x}, y={pos_y}")
    if fill_color:
        content_parts.append(f"Color: {fill_color}")

    metadata: Dict[str, Any] = {
        "item_id": item_id,
        "item_type": item_type,
        "board_id": board_id,
        "content": content_text,
        "created_at": created_at,
        "modified_at": modified_at,
        "created_by": created_by,
        "modified_by": modified_by,
        "position_x": pos_x,
        "position_y": pos_y,
        "fill_color": fill_color,
        "object_type": "item",
        "source": "miro",
    }

    return ConnectorDocument(
        id=stable,
        title=title,
        content="\n".join(content_parts),
        type=f"miro_{item_type}",
        metadata=metadata,
    )


# ── retry ─────────────────────────────────────────────────────────────────────

async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Auth errors are not retried — they require re-authorization.
    NotFound errors are not retried — the resource does not exist.
    RateLimit and network errors are retried up to max_attempts times.
    """
    from exceptions import MiroAuthError, MiroError, MiroNotFoundError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except MiroAuthError:
            raise
        except MiroNotFoundError:
            raise
        except MiroError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
