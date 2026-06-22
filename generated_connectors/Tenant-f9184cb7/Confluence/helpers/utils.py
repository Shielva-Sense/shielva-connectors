from __future__ import annotations

import asyncio
import hashlib
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ConfluenceAuthError, ConfluenceError, ConfluenceRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _strip_html(text: str) -> str:
    """Remove HTML/XML tags and decode basic entities from Confluence storage format."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&amp;", "&").replace("&quot;", '"').replace("&apos;", "'")
    text = text.replace("&nbsp;", " ")
    return " ".join(text.split()).strip()


def _extract_next_cursor(response: dict[str, Any]) -> str | None:
    """Extract the next-page cursor from a Confluence v2 _links.next URL."""
    links: dict[str, Any] = response.get("_links") or {}
    next_url: str = links.get("next", "")
    if not next_url:
        return None
    # next_url is like /wiki/api/v2/spaces?cursor=abc123&limit=50
    # Extract the cursor query param
    match = re.search(r"[?&]cursor=([^&]+)", next_url)
    if match:
        return match.group(1)
    return None


def normalize_page(
    page: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    domain: str = "",
) -> ConnectorDocument:
    """Normalize a raw Confluence page dict into a ConnectorDocument.

    The stable ``id`` is the first 16 hex characters of SHA-256(page_id).
    ``type`` is always ``"confluence_page"``.
    """
    page_id: str = str(page.get("id", ""))
    title: str = page.get("title", "") or f"Page {page_id}"

    # Stable ID — SHA-256 of the page id, truncated to 16 hex chars
    stable_id = hashlib.sha256(page_id.encode()).hexdigest()[:16]

    # Body content — present when fetched with body-format=storage
    body_obj: dict[str, Any] = page.get("body") or {}
    storage_obj: dict[str, Any] = body_obj.get("storage") or {}
    raw_content: str = storage_obj.get("value", "")
    plain_content = _strip_html(raw_content) if raw_content else ""

    # Space info
    space_id: str = str(page.get("spaceId", ""))

    # Author info
    version_obj: dict[str, Any] = page.get("version") or {}
    author_obj: dict[str, Any] = version_obj.get("authorId") or {}
    author: str = ""
    if isinstance(author_obj, dict):
        author = author_obj.get("displayName", "")
    elif isinstance(author_obj, str):
        author = author_obj

    created_at: str = page.get("createdAt", "")
    updated_at: str = version_obj.get("createdAt", "")

    # Build content text
    content_parts = [f"Title: {title}"]
    if space_id:
        content_parts.append(f"Space ID: {space_id}")
    if author:
        content_parts.append(f"Author: {author}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")
    if plain_content:
        content_parts.append(f"Content: {plain_content}")
    content = "\n".join(content_parts)

    # Source URL
    source_url = ""
    if domain and page_id:
        source_url = f"https://{domain}.atlassian.net/wiki/spaces/{space_id}/pages/{page_id}"

    metadata: dict[str, Any] = {
        "page_id": page_id,
        "space_id": space_id,
        "author": author,
        "created_at": created_at,
        "updated_at": updated_at,
        "status": page.get("status", ""),
    }

    return ConnectorDocument(
        id=stable_id,
        source_id=page_id,
        title=title,
        content=content,
        type="confluence_page",
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata=metadata,
    )


def normalize_blog_post(
    post: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    domain: str = "",
) -> ConnectorDocument:
    """Normalize a raw Confluence blog post dict into a ConnectorDocument.

    The stable ``id`` is the first 16 hex characters of SHA-256(post_id).
    ``type`` is always ``"confluence_blog_post"``.
    """
    post_id: str = str(post.get("id", ""))
    title: str = post.get("title", "") or f"Blog Post {post_id}"

    stable_id = hashlib.sha256(post_id.encode()).hexdigest()[:16]

    body_obj: dict[str, Any] = post.get("body") or {}
    storage_obj: dict[str, Any] = body_obj.get("storage") or {}
    raw_content: str = storage_obj.get("value", "")
    plain_content = _strip_html(raw_content) if raw_content else ""

    space_id: str = str(post.get("spaceId", ""))

    version_obj: dict[str, Any] = post.get("version") or {}
    created_at: str = post.get("createdAt", "")
    updated_at: str = version_obj.get("createdAt", "")

    content_parts = [f"Title: {title}", "Type: Blog Post"]
    if space_id:
        content_parts.append(f"Space ID: {space_id}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")
    if plain_content:
        content_parts.append(f"Content: {plain_content}")
    content = "\n".join(content_parts)

    source_url = ""
    if domain and post_id:
        source_url = f"https://{domain}.atlassian.net/wiki/spaces/{space_id}/blog/{post_id}"

    metadata: dict[str, Any] = {
        "post_id": post_id,
        "space_id": space_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "status": post.get("status", ""),
    }

    return ConnectorDocument(
        id=stable_id,
        source_id=post_id,
        title=title,
        content=content,
        type="confluence_blog_post",
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata=metadata,
    )


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: ConfluenceError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ConfluenceAuthError:
            raise
        except ConfluenceRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ConfluenceError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
