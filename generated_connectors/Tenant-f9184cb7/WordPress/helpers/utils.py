from __future__ import annotations

import asyncio
import hashlib
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import WordPressAuthError, WordPressError, WordPressRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(prefix: str, raw_id: int | str) -> str:
    """Return first 16 hex chars of SHA-256 of '<prefix>:<raw_id>'."""
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


def _strip_tags(html: str) -> str:
    """Remove HTML tags from a string without external dependencies."""
    text = re.sub(r"<[^>]+>", "", html or "")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _rendered(field: Any) -> str:
    """Extract the 'rendered' key from a WordPress rendered object, or cast to str."""
    if isinstance(field, dict):
        return field.get("rendered", "") or ""
    return str(field) if field else ""


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential back-off + jitter.

    Auth errors are never retried — they require human intervention.
    """
    last_exc: WordPressError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except WordPressAuthError:
            raise
        except WordPressRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except WordPressError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_post(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    site_url: str = "",
) -> ConnectorDocument:
    """Convert a raw WordPress Post object into a ConnectorDocument."""
    post_id: int = raw.get("id", 0)
    title: str = _strip_tags(_rendered(raw.get("title", "")))
    content: str = _strip_tags(_rendered(raw.get("content", "")))
    excerpt: str = _strip_tags(_rendered(raw.get("excerpt", "")))
    status: str = raw.get("status", "")
    date: str = raw.get("date", "")
    modified: str = raw.get("modified", "")
    author: int = raw.get("author", 0)
    link: str = raw.get("link", "")
    slug: str = raw.get("slug", "")
    post_type: str = raw.get("type", "post")
    comment_status: str = raw.get("comment_status", "")

    categories: list[int] = raw.get("categories", [])
    tags: list[int] = raw.get("tags", [])

    content_parts: list[str] = [
        f"Title: {title}",
        f"Status: {status}",
        f"Date: {date}",
        f"Modified: {modified}",
        f"Author ID: {author}",
        f"Slug: {slug}",
        f"Type: {post_type}",
    ]
    if excerpt:
        content_parts.append(f"Excerpt: {excerpt[:300]}")
    if content:
        content_parts.append(f"Content: {content[:1000]}")
    if categories:
        content_parts.append(f"Category IDs: {', '.join(str(c) for c in categories)}")
    if tags:
        content_parts.append(f"Tag IDs: {', '.join(str(t) for t in tags)}")

    return ConnectorDocument(
        source_id=_stable_id("post", post_id),
        title=title or f"Post {post_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=link or f"{site_url.rstrip('/')}/?p={post_id}",
        metadata={
            "post_id": post_id,
            "status": status,
            "date": date,
            "modified": modified,
            "author": author,
            "slug": slug,
            "type": post_type,
            "categories": categories,
            "tags": tags,
            "comment_status": comment_status,
        },
    )


def normalize_page(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    site_url: str = "",
) -> ConnectorDocument:
    """Convert a raw WordPress Page object into a ConnectorDocument."""
    page_id: int = raw.get("id", 0)
    title: str = _strip_tags(_rendered(raw.get("title", "")))
    content: str = _strip_tags(_rendered(raw.get("content", "")))
    excerpt: str = _strip_tags(_rendered(raw.get("excerpt", "")))
    status: str = raw.get("status", "")
    date: str = raw.get("date", "")
    modified: str = raw.get("modified", "")
    author: int = raw.get("author", 0)
    link: str = raw.get("link", "")
    slug: str = raw.get("slug", "")
    menu_order: int = raw.get("menu_order", 0)
    parent: int = raw.get("parent", 0)
    template: str = raw.get("template", "")

    content_parts: list[str] = [
        f"Title: {title}",
        f"Status: {status}",
        f"Date: {date}",
        f"Modified: {modified}",
        f"Author ID: {author}",
        f"Slug: {slug}",
        f"Menu Order: {menu_order}",
    ]
    if parent:
        content_parts.append(f"Parent Page ID: {parent}")
    if template:
        content_parts.append(f"Template: {template}")
    if excerpt:
        content_parts.append(f"Excerpt: {excerpt[:300]}")
    if content:
        content_parts.append(f"Content: {content[:1000]}")

    return ConnectorDocument(
        source_id=_stable_id("page", page_id),
        title=title or f"Page {page_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=link or f"{site_url.rstrip('/')}/?page_id={page_id}",
        metadata={
            "page_id": page_id,
            "status": status,
            "date": date,
            "modified": modified,
            "author": author,
            "slug": slug,
            "menu_order": menu_order,
            "parent": parent,
            "template": template,
        },
    )


def normalize_user(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    site_url: str = "",
) -> ConnectorDocument:
    """Convert a raw WordPress User object into a ConnectorDocument."""
    user_id: int = raw.get("id", 0)
    name: str = raw.get("name", "")
    slug: str = raw.get("slug", "")
    email: str = raw.get("email", "")
    url: str = raw.get("url", "")
    description: str = _strip_tags(_rendered(raw.get("description", "")))
    registered_date: str = raw.get("registered_date", "")
    roles: list[str] = raw.get("roles", [])
    link: str = raw.get("link", "")
    avatar_urls: dict[str, str] = raw.get("avatar_urls", {})

    content_parts: list[str] = [
        f"Name: {name}",
        f"Username (slug): {slug}",
    ]
    if email:
        content_parts.append(f"Email: {email}")
    if url:
        content_parts.append(f"Website: {url}")
    if roles:
        content_parts.append(f"Roles: {', '.join(roles)}")
    if registered_date:
        content_parts.append(f"Registered: {registered_date}")
    if description:
        content_parts.append(f"Bio: {description[:500]}")

    avatar_url: str = avatar_urls.get("96", avatar_urls.get("48", ""))

    return ConnectorDocument(
        source_id=_stable_id("user", user_id),
        title=name or slug or f"User {user_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=link or f"{site_url.rstrip('/')}/author/{slug}/",
        metadata={
            "user_id": user_id,
            "slug": slug,
            "email": email,
            "roles": roles,
            "registered_date": registered_date,
            "avatar_url": avatar_url,
        },
    )


def normalize_media(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    site_url: str = "",
) -> ConnectorDocument:
    """Convert a raw WordPress Media object into a ConnectorDocument."""
    media_id: int = raw.get("id", 0)
    title: str = _strip_tags(_rendered(raw.get("title", "")))
    caption: str = _strip_tags(_rendered(raw.get("caption", "")))
    alt_text: str = raw.get("alt_text", "")
    description: str = _strip_tags(_rendered(raw.get("description", "")))
    media_type: str = raw.get("media_type", "")
    mime_type: str = raw.get("mime_type", "")
    source_url_raw: str = raw.get("source_url", "")
    link: str = raw.get("link", "")
    date: str = raw.get("date", "")
    author: int = raw.get("author", 0)
    slug: str = raw.get("slug", "")

    # Media details (sizes, dimensions)
    media_details: dict[str, Any] = raw.get("media_details", {})
    width: int = media_details.get("width", 0)
    height: int = media_details.get("height", 0)
    file_name: str = media_details.get("file", "")

    content_parts: list[str] = [
        f"Title: {title}",
        f"Media Type: {media_type}",
        f"MIME Type: {mime_type}",
        f"Date: {date}",
        f"Author ID: {author}",
        f"File: {source_url_raw}",
    ]
    if width and height:
        content_parts.append(f"Dimensions: {width}x{height}")
    if alt_text:
        content_parts.append(f"Alt Text: {alt_text}")
    if caption:
        content_parts.append(f"Caption: {caption[:300]}")
    if description:
        content_parts.append(f"Description: {description[:300]}")

    return ConnectorDocument(
        source_id=_stable_id("media", media_id),
        title=title or file_name or f"Media {media_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url_raw or link,
        metadata={
            "media_id": media_id,
            "media_type": media_type,
            "mime_type": mime_type,
            "date": date,
            "author": author,
            "slug": slug,
            "width": width,
            "height": height,
            "alt_text": alt_text,
            "source_url": source_url_raw,
        },
    )


def normalize_category(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    site_url: str = "",
) -> ConnectorDocument:
    """Convert a raw WordPress Category (or Tag) object into a ConnectorDocument."""
    cat_id: int = raw.get("id", 0)
    name: str = raw.get("name", "")
    slug: str = raw.get("slug", "")
    description: str = _strip_tags(_rendered(raw.get("description", "")))
    count: int = raw.get("count", 0)
    link: str = raw.get("link", "")
    taxonomy: str = raw.get("taxonomy", "category")
    parent: int = raw.get("parent", 0)

    content_parts: list[str] = [
        f"Name: {name}",
        f"Taxonomy: {taxonomy}",
        f"Slug: {slug}",
        f"Post Count: {count}",
    ]
    if parent:
        content_parts.append(f"Parent ID: {parent}")
    if description:
        content_parts.append(f"Description: {description[:500]}")

    return ConnectorDocument(
        source_id=_stable_id(taxonomy, cat_id),
        title=name or slug or f"{taxonomy.capitalize()} {cat_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=link or f"{site_url.rstrip('/')}/?{taxonomy}={slug}",
        metadata={
            "category_id": cat_id,
            "name": name,
            "slug": slug,
            "taxonomy": taxonomy,
            "count": count,
            "parent": parent,
        },
    )
