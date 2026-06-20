"""Shared utilities: normalize_file, normalize_drive, retry logic, stable ID generation."""
from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, Dict, List, Optional, TypeVar

from exceptions import GoogleDriveAuthError, GoogleDriveError, GoogleDriveRateLimitError
from models import ConnectorDocument

# OCP: retry constants — change here, nowhere else
RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

_FOLDER_MIME = "application/vnd.google-apps.folder"

# Google native MIME types that cannot be downloaded but can be exported
_GOOGLE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.spreadsheet",
        "application/vnd.google-apps.presentation",
        "application/vnd.google-apps.drawing",
    }
)

# Human-readable type labels for known MIME types
_MIME_TYPE_LABELS: Dict[str, str] = {
    _FOLDER_MIME: "folder",
    "application/vnd.google-apps.document": "google_doc",
    "application/vnd.google-apps.spreadsheet": "google_sheet",
    "application/vnd.google-apps.presentation": "google_slide",
    "application/vnd.google-apps.drawing": "google_drawing",
    "application/pdf": "pdf",
    "image/jpeg": "image",
    "image/png": "image",
    "image/gif": "image",
    "text/plain": "text",
    "text/csv": "csv",
    "application/zip": "archive",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


def is_folder(mime_type: str) -> bool:
    """Return True when the MIME type represents a Google Drive folder."""
    return mime_type == _FOLDER_MIME


def get_file_type(mime_type: str) -> str:
    """Return a human-readable file type label for the given MIME type.

    Falls back to "file" for unknown MIME types.
    """
    return _MIME_TYPE_LABELS.get(mime_type, "file")


def _stable_id(file_id: str) -> str:
    """Return first 16 hex characters of SHA-256('google_drive:{file_id}') as a stable short ID."""
    return hashlib.sha256(f"google_drive:{file_id}".encode()).hexdigest()[:16]


def normalize_file(
    raw: Dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Google Drive files.list entry to a ConnectorDocument.

    The document `id` is namespaced by connector_id for uniqueness. The stable
    short ID is stored in `source_id` so callers can re-derive it from file_id.
    For folders, `type` is set to "folder"; for other files, `type` is set to "file"
    or the specific Google type label.
    """
    file_id: str = raw.get("id", "")
    name: str = raw.get("name", "(untitled)")
    mime_type: str = raw.get("mimeType", "")
    web_view_link: str = raw.get("webViewLink", "")
    description: str = raw.get("description", "")
    created_time: str = raw.get("createdTime", "")
    modified_time: str = raw.get("modifiedTime", "")
    size: Optional[str] = raw.get("size")
    trashed: bool = raw.get("trashed", False)
    parents: List[str] = raw.get("parents", [])
    shared: bool = raw.get("shared", False)
    starred: bool = raw.get("starred", False)
    owners_raw: List[Dict[str, Any]] = raw.get("owners", [])
    owner_email: str = owners_raw[0].get("emailAddress", "") if owners_raw else ""

    doc_type = "folder" if is_folder(mime_type) else "file"

    content_parts: List[str] = [f"Name: {name}"]
    if description:
        content_parts.append(f"Description: {description}")
    if mime_type:
        content_parts.append(f"Type: {mime_type}")
    if created_time:
        content_parts.append(f"Created: {created_time}")
    if modified_time:
        content_parts.append(f"Modified: {modified_time}")
    content = "\n".join(content_parts)

    stable = _stable_id(file_id) if file_id else ""
    doc_id = f"{connector_id}_{stable}" if connector_id and stable else stable

    return ConnectorDocument(
        id=doc_id,
        source_id=file_id,
        title=name,
        content=content,
        source_url=web_view_link,
        author=owner_email,
        connector_id=connector_id,
        tenant_id=tenant_id,
        type=doc_type,
        metadata={
            "file_id": file_id,
            "mime_type": mime_type,
            "parents": parents,
            "owners": owners_raw,
            "size": size,
            "trashed": trashed,
            "shared": shared,
            "starred": starred,
            "created_time": created_time,
            "modified_time": modified_time,
            "is_google_doc": mime_type in _GOOGLE_MIME_TYPES,
        },
    )


def normalize_drive(
    raw: Dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Google Drive /drives entry to a ConnectorDocument.

    Shared drives have their own id, name, and kind fields.
    """
    drive_id: str = raw.get("id", "")
    name: str = raw.get("name", "(untitled drive)")
    kind: str = raw.get("kind", "drive#drive")

    stable = _stable_id(drive_id) if drive_id else ""
    doc_id = f"{connector_id}_{stable}" if connector_id and stable else stable

    content_parts: List[str] = [
        f"Name: {name}",
        f"Type: Shared Drive",
        f"Kind: {kind}",
    ]
    content = "\n".join(content_parts)

    return ConnectorDocument(
        id=doc_id,
        source_id=drive_id,
        title=name,
        content=content,
        source_url="",
        author="",
        connector_id=connector_id,
        tenant_id=tenant_id,
        type="shared_drive",
        metadata={
            "drive_id": drive_id,
            "kind": kind,
        },
    )


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except GoogleDriveAuthError:
            raise
        except GoogleDriveRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except GoogleDriveError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
