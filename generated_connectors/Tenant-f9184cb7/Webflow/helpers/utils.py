"""Webflow connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, List, Optional

from models import ConnectorDocument, WebflowResourceType


def _stable_id(prefix: str, raw_id: str) -> str:
    """Produce a stable 16-char hex id from a prefix + source id."""
    digest = hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()
    return digest[:16]


# ── site ──────────────────────────────────────────────────────────────────────


def normalize_site(raw: Dict[str, Any]) -> ConnectorDocument:
    """Convert a Webflow site object to a ConnectorDocument.

    Stable id = sha256("site:" + id)[:16].
    """
    site_id: str = raw.get("id", "")
    stable = _stable_id("site", site_id)

    display_name: str = raw.get("displayName", raw.get("name", "Unnamed Site"))
    short_name: str = raw.get("shortName", "")
    preview_url: str = raw.get("previewUrl", "")
    created_on: str = raw.get("createdOn", "")
    last_updated: str = raw.get("lastUpdated", "")
    time_zone: str = raw.get("timeZone", "")

    content_parts: List[str] = [f"Site: {display_name}"]
    if short_name:
        content_parts.append(f"Short name: {short_name}")
    if preview_url:
        content_parts.append(f"Preview URL: {preview_url}")
    if time_zone:
        content_parts.append(f"Time zone: {time_zone}")
    if created_on:
        content_parts.append(f"Created: {created_on}")
    if last_updated:
        content_parts.append(f"Last updated: {last_updated}")

    metadata: Dict[str, Any] = {
        "site_id": site_id,
        "display_name": display_name,
        "short_name": short_name,
        "preview_url": preview_url,
        "created_on": created_on,
        "last_updated": last_updated,
        "time_zone": time_zone,
        "object_type": "site",
        "source": "webflow",
    }

    return ConnectorDocument(
        id=stable,
        title=display_name,
        content="\n".join(content_parts),
        type=WebflowResourceType.SITE.value,
        metadata=metadata,
    )


# ── collection ────────────────────────────────────────────────────────────────


def normalize_collection(raw: Dict[str, Any], site_id: str) -> ConnectorDocument:
    """Convert a Webflow collection object to a ConnectorDocument.

    Stable id = sha256("collection:" + id)[:16].
    """
    coll_id: str = raw.get("id", "")
    stable = _stable_id("collection", coll_id)

    display_name: str = raw.get("displayName", raw.get("name", "Unnamed Collection"))
    slug: str = raw.get("slug", "")
    singular_name: str = raw.get("singularName", "")
    created_on: str = raw.get("createdOn", "")
    last_updated: str = raw.get("lastUpdated", "")

    # Field schema summary
    fields: List[Dict[str, Any]] = raw.get("fields", [])
    field_names: List[str] = [f.get("displayName", f.get("slug", "")) for f in fields if f]

    content_parts: List[str] = [f"Collection: {display_name}"]
    if slug:
        content_parts.append(f"Slug: {slug}")
    if singular_name:
        content_parts.append(f"Singular name: {singular_name}")
    if field_names:
        content_parts.append(f"Fields: {', '.join(field_names)}")
    if created_on:
        content_parts.append(f"Created: {created_on}")
    if last_updated:
        content_parts.append(f"Last updated: {last_updated}")

    metadata: Dict[str, Any] = {
        "collection_id": coll_id,
        "site_id": site_id,
        "display_name": display_name,
        "slug": slug,
        "singular_name": singular_name,
        "field_count": len(fields),
        "field_names": field_names,
        "created_on": created_on,
        "last_updated": last_updated,
        "object_type": "collection",
        "source": "webflow",
    }

    return ConnectorDocument(
        id=stable,
        title=display_name,
        content="\n".join(content_parts),
        type=WebflowResourceType.COLLECTION.value,
        metadata=metadata,
    )


# ── item ──────────────────────────────────────────────────────────────────────


def normalize_item(raw: Dict[str, Any], collection_id: str) -> ConnectorDocument:
    """Convert a Webflow CMS item object to a ConnectorDocument.

    Stable id = sha256("item:" + id)[:16].
    """
    item_id: str = raw.get("id", "")
    stable = _stable_id("item", item_id)

    # Field data lives under "fieldData" in Webflow API v2
    field_data: Dict[str, Any] = raw.get("fieldData", {})
    title: str = (
        field_data.get("name", "")
        or field_data.get("title", "")
        or field_data.get("Name", "")
        or "Untitled Item"
    )
    slug: str = field_data.get("slug", "")

    is_archived: bool = raw.get("isArchived", False)
    is_draft: bool = raw.get("isDraft", False)
    created_on: str = raw.get("createdOn", "")
    last_updated: str = raw.get("lastUpdated", "")
    last_published: str = raw.get("lastPublished", "")

    content_parts: List[str] = [f"Item: {title}"]
    if slug:
        content_parts.append(f"Slug: {slug}")
    if is_draft:
        content_parts.append("Status: Draft")
    elif is_archived:
        content_parts.append("Status: Archived")
    else:
        content_parts.append("Status: Published")
    if created_on:
        content_parts.append(f"Created: {created_on}")
    if last_updated:
        content_parts.append(f"Last updated: {last_updated}")
    if last_published:
        content_parts.append(f"Last published: {last_published}")

    # Include non-system field data as content
    skip_keys = {"slug", "name", "title", "Name"}
    for key, val in field_data.items():
        if key not in skip_keys and isinstance(val, (str, int, float, bool)):
            content_parts.append(f"{key}: {val}")

    metadata: Dict[str, Any] = {
        "item_id": item_id,
        "collection_id": collection_id,
        "title": title,
        "slug": slug,
        "is_archived": is_archived,
        "is_draft": is_draft,
        "created_on": created_on,
        "last_updated": last_updated,
        "last_published": last_published,
        "object_type": "item",
        "source": "webflow",
    }

    return ConnectorDocument(
        id=stable,
        title=title,
        content="\n".join(content_parts),
        type=WebflowResourceType.ITEM.value,
        metadata=metadata,
    )


# ── page ──────────────────────────────────────────────────────────────────────


def normalize_page(raw: Dict[str, Any], site_id: str) -> ConnectorDocument:
    """Convert a Webflow page object to a ConnectorDocument.

    Stable id = sha256("page:" + id)[:16].
    """
    page_id: str = raw.get("id", "")
    stable = _stable_id("page", page_id)

    title: str = raw.get("title", "Untitled Page")
    slug: str = raw.get("slug", "")
    draft: bool = raw.get("draft", False)
    archived: bool = raw.get("archived", False)
    created_on: str = raw.get("createdOn", "")
    last_updated: str = raw.get("lastUpdated", "")
    seo_settings: Dict[str, Any] = raw.get("seo", {})
    meta_title: str = seo_settings.get("title", "")
    meta_desc: str = seo_settings.get("description", "")
    open_graph: Dict[str, Any] = raw.get("openGraph", {})
    og_title: str = open_graph.get("title", "")

    content_parts: List[str] = [f"Page: {title}"]
    if slug:
        content_parts.append(f"Slug: /{slug}")
    if draft:
        content_parts.append("Status: Draft")
    elif archived:
        content_parts.append("Status: Archived")
    if meta_title:
        content_parts.append(f"SEO title: {meta_title}")
    if meta_desc:
        content_parts.append(f"SEO description: {meta_desc}")
    if og_title:
        content_parts.append(f"OG title: {og_title}")
    if created_on:
        content_parts.append(f"Created: {created_on}")
    if last_updated:
        content_parts.append(f"Last updated: {last_updated}")

    metadata: Dict[str, Any] = {
        "page_id": page_id,
        "site_id": site_id,
        "title": title,
        "slug": slug,
        "draft": draft,
        "archived": archived,
        "meta_title": meta_title,
        "meta_description": meta_desc,
        "created_on": created_on,
        "last_updated": last_updated,
        "object_type": "page",
        "source": "webflow",
    }

    return ConnectorDocument(
        id=stable,
        title=title,
        content="\n".join(content_parts),
        type=WebflowResourceType.PAGE.value,
        metadata=metadata,
    )


# ── with_retry ────────────────────────────────────────────────────────────────


async def with_retry(
    fn: Callable[[], Any],
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on WebflowAuthError — re-authorizing is required.
    Skips retry on WebflowNotFoundError — the resource does not exist.
    """
    from exceptions import WebflowAuthError, WebflowError, WebflowNotFoundError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                return await result
            return result
        except WebflowAuthError:
            raise
        except WebflowNotFoundError:
            raise
        except WebflowError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
