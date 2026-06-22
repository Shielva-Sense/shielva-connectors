"""Domo connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, List, Optional

from models import ConnectorDocument


# ── ID helpers ────────────────────────────────────────────────────────────────

def _stable_id(prefix: str, raw_id: str) -> str:
    """Return a 16-char stable document ID: sha256('<prefix>:<raw_id>')[:16]."""
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


# ── normalizers ───────────────────────────────────────────────────────────────

def normalize_dataset(raw: Dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Domo dataset dict into a ConnectorDocument.

    Stable id = sha256("dataset:" + str(dataset_id))[:16].
    """
    dataset_id = str(raw.get("id", ""))
    stable = _stable_id("dataset", dataset_id)

    name = raw.get("name", "Untitled Dataset")
    description = raw.get("description", "") or ""
    row_count = raw.get("rows", raw.get("rowCount", 0)) or 0
    column_count = raw.get("columns", raw.get("columnCount", 0)) or 0
    created_at = str(raw.get("createdAt", raw.get("created", "")))
    updated_at = str(raw.get("updatedAt", raw.get("updated", raw.get("lastUpdated", ""))))
    owner_obj = raw.get("owner", {})
    owner_name: str = ""
    if isinstance(owner_obj, dict):
        owner_name = owner_obj.get("name", "")
    data_source: str = raw.get("dataSource", {}).get("type", "") if isinstance(raw.get("dataSource"), dict) else ""
    status: str = str(raw.get("status", ""))

    content_parts: List[str] = [f"Dataset: {name}"]
    if description:
        content_parts.append(f"Description: {description}")
    if row_count:
        content_parts.append(f"Rows: {row_count}")
    if column_count:
        content_parts.append(f"Columns: {column_count}")
    if owner_name:
        content_parts.append(f"Owner: {owner_name}")
    if data_source:
        content_parts.append(f"Data Source Type: {data_source}")
    if status:
        content_parts.append(f"Status: {status}")
    if created_at and created_at != "None":
        content_parts.append(f"Created: {created_at}")
    if updated_at and updated_at != "None":
        content_parts.append(f"Updated: {updated_at}")

    metadata: Dict[str, Any] = {
        "dataset_id": dataset_id,
        "row_count": row_count,
        "column_count": column_count,
        "owner_name": owner_name,
        "data_source_type": data_source,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "source": "domo",
        "resource_type": "dataset",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="dataset",
        metadata=metadata,
    )


def normalize_page(raw: Dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Domo page (dashboard) dict into a ConnectorDocument.

    Stable id = sha256("page:" + str(page_id))[:16].
    """
    page_id = str(raw.get("id", ""))
    stable = _stable_id("page", page_id)

    name = raw.get("name", "Untitled Dashboard")
    parent_id = raw.get("parentId", raw.get("parent_id", None))
    card_count = raw.get("cardCount", raw.get("card_count", 0)) or 0
    visibility = raw.get("visibility", raw.get("type", ""))
    collection_ids: List[str] = [str(c) for c in raw.get("collectionIds", [])]
    description: str = raw.get("description", "") or ""

    content_parts: List[str] = [f"Dashboard: {name}"]
    if description:
        content_parts.append(f"Description: {description}")
    if parent_id is not None:
        content_parts.append(f"Parent Page ID: {parent_id}")
    if card_count:
        content_parts.append(f"Cards: {card_count}")
    if visibility:
        content_parts.append(f"Visibility: {visibility}")
    if collection_ids:
        content_parts.append(f"Collections: {', '.join(collection_ids)}")

    metadata: Dict[str, Any] = {
        "page_id": page_id,
        "parent_id": str(parent_id) if parent_id is not None else "",
        "card_count": card_count,
        "visibility": visibility,
        "collection_ids": collection_ids,
        "source": "domo",
        "resource_type": "dashboard",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="dashboard",
        metadata=metadata,
    )


def normalize_user(raw: Dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Domo user dict into a ConnectorDocument.

    Stable id = sha256("user:" + str(user_id))[:16].
    """
    user_id = str(raw.get("id", ""))
    stable = _stable_id("user", user_id)

    name = raw.get("name", raw.get("displayName", "Unknown User"))
    email = raw.get("email", raw.get("emailAddress", ""))
    role = raw.get("role", "")
    title = raw.get("title", "")
    department = raw.get("department", "")
    phone = raw.get("phone", raw.get("phoneNumber", ""))
    location = raw.get("location", "")
    created_at = str(raw.get("createdAt", raw.get("created", "")))

    content_parts: List[str] = [f"User: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if role:
        content_parts.append(f"Role: {role}")
    if title:
        content_parts.append(f"Title: {title}")
    if department:
        content_parts.append(f"Department: {department}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if location:
        content_parts.append(f"Location: {location}")
    if created_at and created_at != "None":
        content_parts.append(f"Created: {created_at}")

    metadata: Dict[str, Any] = {
        "user_id": user_id,
        "email": email,
        "role": role,
        "title": title,
        "department": department,
        "phone": phone,
        "location": location,
        "created_at": created_at,
        "source": "domo",
        "resource_type": "user",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="user",
        metadata=metadata,
    )


# ── retry helper ──────────────────────────────────────────────────────────────

async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on DomoAuthError — re-authorizing is required.
    Skips retry on DomoNotFoundError — the resource does not exist.
    """
    from exceptions import DomoAuthError, DomoError, DomoNotFoundError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except DomoAuthError:
            raise
        except DomoNotFoundError:
            raise
        except DomoError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
