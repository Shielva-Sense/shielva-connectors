"""Shared utilities: file/folder normalization, retry logic with exponential backoff.

Imports only from stdlib and exceptions (bare module name — loaded by gateway
sys.path injection). No shared.* imports here to keep this module self-contained.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
from typing import Any, Callable, Coroutine, Dict, Optional

import structlog

from exceptions import BoxRateLimitError, BoxNetworkError
from models import ConnectorDocument

logger = structlog.get_logger(__name__)

# OCP: retry constants — change here, nowhere else
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    coro_fn: Callable[[], Coroutine[Any, Any, Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> Any:
    """Execute *coro_fn()* with exponential-backoff retry.

    Retries on BoxRateLimitError and BoxNetworkError.
    Raises the last exception after exhausting all retries.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except BoxRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "box.rate_limit — retrying",
                attempt=attempt + 1,
                delay=delay,
            )
            await asyncio.sleep(delay)
        except BoxNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "box.network_error — retrying",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def _stable_id(prefix: str, item_id: str) -> str:
    """Return a 16-char SHA-256 hex prefix as a stable document ID.

    Format: SHA-256("{prefix}:{item_id}")[:16]
    """
    raw = f"{prefix}:{item_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_file(
    item: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Box API file dict to a ConnectorDocument.

    Title    = file name.
    Content  = description + key metadata as human-readable text.
    Metadata = type, size, parent, sha1, modified_at, created_at, owned_by.
    ID       = SHA-256("file:{file_id}")[:16]
    """
    file_id: str = item.get("id", "")
    name: str = item.get("name", "") or "(unnamed)"
    description: str = item.get("description", "") or ""
    size: int = item.get("size", 0) or 0
    modified_at: str = item.get("modified_at", "") or ""
    created_at: str = item.get("created_at", "") or ""
    sha1: str = item.get("sha1", "") or ""

    parent: Dict[str, Any] = item.get("parent") or {}
    parent_id: str = parent.get("id", "") or ""
    parent_name: str = parent.get("name", "") or ""

    owned_by: Dict[str, Any] = item.get("owned_by") or {}
    owned_by_name: str = owned_by.get("name", "") or ""
    owned_by_login: str = owned_by.get("login", "") or ""
    owner_str: str = owned_by_name or owned_by_login

    shared_link: Optional[Dict[str, Any]] = item.get("shared_link")
    shared_url: str = (shared_link or {}).get("url", "") or ""

    # Build human-readable content block
    content_parts = []
    if description:
        content_parts.append(description)
    if name:
        content_parts.append(f"File: {name}")
    if size:
        content_parts.append(f"Size: {size} bytes")
    if modified_at:
        content_parts.append(f"Modified: {modified_at}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if owner_str:
        content_parts.append(f"Owner: {owner_str}")
    if parent_name:
        content_parts.append(f"Folder: {parent_name}")
    content: str = "\n".join(content_parts)

    metadata: Dict[str, Any] = {
        "type": "file",
        "file_id": file_id,
        "name": name,
        "size": size,
        "modified_at": modified_at,
        "created_at": created_at,
        "sha1": sha1,
        "parent_id": parent_id,
        "parent_name": parent_name,
        "owned_by": owner_str,
        "owned_by_login": owned_by_login,
        "shared_url": shared_url,
        "description": description,
    }

    return ConnectorDocument(
        id=_stable_id("file", file_id),
        source="box",
        title=name,
        content=content,
        metadata=metadata,
        connector_id=connector_id,
        tenant_id=tenant_id,
    )


def normalize_folder(
    item: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Box API folder dict to a ConnectorDocument.

    Title    = folder name.
    Content  = description + key metadata as human-readable text.
    Metadata = type, parent, owned_by, modified_at, created_at.
    ID       = SHA-256("folder:{folder_id}")[:16]
    """
    folder_id: str = item.get("id", "")
    name: str = item.get("name", "") or "(unnamed folder)"
    description: str = item.get("description", "") or ""
    modified_at: str = item.get("modified_at", "") or ""
    created_at: str = item.get("created_at", "") or ""

    parent: Dict[str, Any] = item.get("parent") or {}
    parent_id: str = parent.get("id", "") or ""
    parent_name: str = parent.get("name", "") or ""

    owned_by: Dict[str, Any] = item.get("owned_by") or {}
    owned_by_name: str = owned_by.get("name", "") or ""
    owned_by_login: str = owned_by.get("login", "") or ""
    owner_str: str = owned_by_name or owned_by_login

    # Build human-readable content block
    content_parts = []
    if description:
        content_parts.append(description)
    if name:
        content_parts.append(f"Folder: {name}")
    if modified_at:
        content_parts.append(f"Modified: {modified_at}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if owner_str:
        content_parts.append(f"Owner: {owner_str}")
    if parent_name:
        content_parts.append(f"Parent: {parent_name}")
    content: str = "\n".join(content_parts)

    metadata: Dict[str, Any] = {
        "type": "folder",
        "folder_id": folder_id,
        "name": name,
        "modified_at": modified_at,
        "created_at": created_at,
        "parent_id": parent_id,
        "parent_name": parent_name,
        "owned_by": owner_str,
        "owned_by_login": owned_by_login,
        "description": description,
    }

    return ConnectorDocument(
        id=_stable_id("folder", folder_id),
        source="box",
        title=name,
        content=content,
        metadata=metadata,
        connector_id=connector_id,
        tenant_id=tenant_id,
    )
