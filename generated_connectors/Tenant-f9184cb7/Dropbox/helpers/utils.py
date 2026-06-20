from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import DropboxAuthError, DropboxError, DropboxRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


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
    last_exc: DropboxError | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except DropboxAuthError:
            raise
        except DropboxRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except DropboxError as exc:
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


def _stable_doc_id(dropbox_id: str) -> str:
    """Return first 16 hex chars of SHA-256(dropbox_file_id) as a stable source_id."""
    return hashlib.sha256(dropbox_id.encode()).hexdigest()[:16]


def normalize_file(entry: dict[str, Any]) -> ConnectorDocument:
    """Normalize a Dropbox file entry (.tag == 'file') into a ConnectorDocument.

    id   = SHA-256('file:' + entry.id)[:16]
    source = 'dropbox'
    type   = 'file'
    """
    file_id = entry.get("id", "")
    path_lower = entry.get("path_lower", "")
    stable_key = f"file:{file_id}" if file_id else f"file:{path_lower}"
    source_id = _stable_doc_id(stable_key)

    name = entry.get("name", "")
    path_display = entry.get("path_display", "")
    server_modified = entry.get("server_modified", "")
    client_modified = entry.get("client_modified", "")
    size = entry.get("size", 0)
    rev = entry.get("rev", "")
    is_downloadable = entry.get("is_downloadable", True)

    content_parts = [
        f"Name: {name}",
        f"Path: {path_display}",
        f"Type: file",
    ]
    if size:
        content_parts.append(f"Size: {size} bytes")
    if server_modified:
        content_parts.append(f"Server modified: {server_modified}")
    if client_modified:
        content_parts.append(f"Client modified: {client_modified}")
    if rev:
        content_parts.append(f"Revision: {rev}")

    source_url = f"https://www.dropbox.com/home{path_display}" if path_display else ""

    return ConnectorDocument(
        source_id=source_id,
        title=f"Dropbox file: {path_display or name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "id": source_id,
            "source": "dropbox",
            "type": "file",
            "dropbox_id": file_id,
            "name": name,
            "path_display": path_display,
            "path_lower": path_lower,
            "size": size,
            "server_modified": server_modified,
            "client_modified": client_modified,
            "rev": rev,
            "is_downloadable": is_downloadable,
            "tag": "file",
        },
    )


def normalize_folder(entry: dict[str, Any]) -> ConnectorDocument:
    """Normalize a Dropbox folder entry (.tag == 'folder') into a ConnectorDocument.

    id   = SHA-256('folder:' + entry.id)[:16]
    source = 'dropbox'
    type   = 'folder'
    """
    folder_id = entry.get("id", "")
    path_lower = entry.get("path_lower", "")
    stable_key = f"folder:{folder_id}" if folder_id else f"folder:{path_lower}"
    source_id = _stable_doc_id(stable_key)

    name = entry.get("name", "")
    path_display = entry.get("path_display", "")
    source_url = f"https://www.dropbox.com/home{path_display}" if path_display else ""

    content_parts = [
        f"Name: {name}",
        f"Path: {path_display}",
        f"Type: folder",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"Dropbox folder: {path_display or name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "id": source_id,
            "source": "dropbox",
            "type": "folder",
            "dropbox_id": folder_id,
            "name": name,
            "path_display": path_display,
            "path_lower": path_lower,
            "tag": "folder",
        },
    )


def normalize_file_metadata(
    entry: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a Dropbox file/folder metadata entry into a ConnectorDocument.

    Dropbox entries have a '.tag' field: 'file' or 'folder'.
    Files have a stable '.id' field (id:xxx) that survives renames/moves.
    """
    tag = entry.get(".tag", "file")
    file_id = entry.get("id", "")
    name = entry.get("name", "")
    path_display = entry.get("path_display", "")
    path_lower = entry.get("path_lower", "")
    server_modified = entry.get("server_modified", "")
    client_modified = entry.get("client_modified", "")
    size = entry.get("size", 0)
    rev = entry.get("rev", "")
    is_downloadable = entry.get("is_downloadable", True)

    # Stable source_id: SHA-256[:16] of the Dropbox .id field (stable across moves)
    # Fall back to path_lower if id is absent (folders don't always have id)
    stable_key = file_id if file_id else path_lower
    source_id = _stable_doc_id(stable_key) if stable_key else path_lower

    resource_type = "folder" if tag == "folder" else "file"
    title = f"Dropbox {resource_type}: {path_display or name}"

    content_parts = [
        f"Name: {name}",
        f"Path: {path_display}",
        f"Type: {resource_type}",
    ]
    if tag == "file":
        if size:
            content_parts.append(f"Size: {size} bytes")
        if server_modified:
            content_parts.append(f"Server modified: {server_modified}")
        if client_modified:
            content_parts.append(f"Client modified: {client_modified}")
        if rev:
            content_parts.append(f"Revision: {rev}")

    content = "\n".join(content_parts)

    # Dropbox web link (best-effort; deep link requires sharing API)
    source_url = f"https://www.dropbox.com/home{path_display}" if path_display else ""

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "type": f"dropbox_{resource_type}",
            "dropbox_id": file_id,
            "name": name,
            "path_display": path_display,
            "path_lower": path_lower,
            "size": size,
            "server_modified": server_modified,
            "client_modified": client_modified,
            "rev": rev,
            "is_downloadable": is_downloadable,
            "tag": tag,
        },
    )


class CircuitBreaker:
    """Simple three-state circuit breaker (closed → open → half-open → closed)."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self._failures: int = 0
        self._state: str = "closed"
        self._opened_at: float = 0.0

    @property
    def state(self) -> str:
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                self._state = "half-open"
        return self._state

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()

    def on_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    @property
    def is_open(self) -> bool:
        return self.state == "open"
