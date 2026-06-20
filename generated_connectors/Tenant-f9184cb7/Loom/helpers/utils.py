"""Loom connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, List, Optional

from models import ConnectorDocument


def _stable_id(prefix: str, resource_id: str) -> str:
    """Return a 16-char stable document ID: sha256(prefix:resource_id)[:16]."""
    raw = f"{prefix}:{resource_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_video(
    raw: Dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    transcript: Optional[str] = None,
) -> ConnectorDocument:
    """Convert a raw Loom video dict into a ConnectorDocument.

    Stable ID = sha256("video:" + id)[:16].
    Content prefers transcript if available, otherwise uses description.

    Args:
        raw: raw video dict from the Loom API.
        connector_id: the connector instance ID (for metadata).
        tenant_id: the tenant scope (for metadata).
        transcript: optional transcript text fetched separately.
    """
    video_id = raw.get("id", "")
    stable_id = _stable_id("video", video_id)

    title = raw.get("title", raw.get("name", "Untitled Video"))
    description = raw.get("description", "") or ""
    url = raw.get("url", raw.get("share_url", ""))
    status = raw.get("status", "")
    created_at = raw.get("created_at", raw.get("createdAt", ""))
    updated_at = raw.get("updated_at", raw.get("updatedAt", ""))
    duration = raw.get("duration")
    folder_id = raw.get("folder_id", raw.get("folderId"))
    workspace_id = raw.get("workspace_id", raw.get("workspaceId"))

    # Content: prefer transcript, fall back to description
    if transcript:
        content_body = transcript
    elif description:
        content_body = description
    else:
        content_body = ""

    content_parts: List[str] = [f"Title: {title}"]
    if url:
        content_parts.append(f"URL: {url}")
    if status:
        content_parts.append(f"Status: {status}")
    if duration is not None:
        content_parts.append(f"Duration: {duration}s")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")
    if folder_id:
        content_parts.append(f"Folder ID: {folder_id}")
    if workspace_id:
        content_parts.append(f"Workspace ID: {workspace_id}")
    if content_body:
        content_parts.append(f"\n--- Content ---\n{content_body}")

    metadata: Dict[str, Any] = {
        "video_id": video_id,
        "url": url,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "duration": duration,
        "folder_id": folder_id,
        "workspace_id": workspace_id,
        "has_transcript": bool(transcript),
        "object_type": "video",
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "source": "loom",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="loom_video",
        metadata=metadata,
    )


def normalize_folder(
    raw: Dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Loom folder dict into a ConnectorDocument.

    Stable ID = sha256("folder:" + id)[:16].

    Args:
        raw: raw folder dict from the Loom API.
        connector_id: the connector instance ID.
        tenant_id: the tenant scope.
    """
    folder_id = raw.get("id", "")
    stable_id = _stable_id("folder", folder_id)

    name = raw.get("name", raw.get("title", "Untitled Folder"))
    parent_id = raw.get("parent_id", raw.get("parentId"))
    workspace_id = raw.get("workspace_id", raw.get("workspaceId"))
    created_at = raw.get("created_at", raw.get("createdAt", ""))

    content_parts: List[str] = [f"Folder: {name}"]
    if parent_id:
        content_parts.append(f"Parent Folder ID: {parent_id}")
    if workspace_id:
        content_parts.append(f"Workspace ID: {workspace_id}")
    if created_at:
        content_parts.append(f"Created: {created_at}")

    metadata: Dict[str, Any] = {
        "folder_id": folder_id,
        "name": name,
        "parent_id": parent_id,
        "workspace_id": workspace_id,
        "created_at": created_at,
        "object_type": "folder",
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "source": "loom",
    }

    return ConnectorDocument(
        id=stable_id,
        title=name,
        content="\n".join(content_parts),
        type="loom_folder",
        metadata=metadata,
    )


def normalize_workspace(
    raw: Dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Loom workspace dict into a ConnectorDocument.

    Stable ID = sha256("workspace:" + id)[:16].

    Args:
        raw: raw workspace dict from the Loom API.
        connector_id: the connector instance ID.
        tenant_id: the tenant scope.
    """
    workspace_id = raw.get("id", "")
    stable_id = _stable_id("workspace", workspace_id)

    name = raw.get("name", raw.get("title", "Untitled Workspace"))
    created_at = raw.get("created_at", raw.get("createdAt", ""))
    member_count = raw.get("member_count", raw.get("memberCount", 0))

    content_parts: List[str] = [f"Workspace: {name}"]
    if member_count:
        content_parts.append(f"Members: {member_count}")
    if created_at:
        content_parts.append(f"Created: {created_at}")

    metadata: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "name": name,
        "created_at": created_at,
        "member_count": member_count,
        "object_type": "workspace",
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "source": "loom",
    }

    return ConnectorDocument(
        id=stable_id,
        title=name,
        content="\n".join(content_parts),
        type="loom_workspace",
        metadata=metadata,
    )


async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on LoomAuthError — re-authorizing is required.
    Skips retry on LoomNotFoundError — the resource does not exist.

    Args:
        fn: async callable or coroutine-returning callable to invoke.
        *args: positional arguments forwarded to fn (if any).
        max_attempts: maximum number of total attempts.
        base_delay: base delay in seconds (doubles each attempt).

    Returns:
        The return value of the successful fn call.

    Raises:
        The last exception encountered after exhausting all attempts,
        or LoomAuthError / LoomNotFoundError immediately without retry.
    """
    from exceptions import LoomAuthError, LoomError, LoomNotFoundError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except LoomAuthError:
            raise
        except LoomNotFoundError:
            raise
        except LoomError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
