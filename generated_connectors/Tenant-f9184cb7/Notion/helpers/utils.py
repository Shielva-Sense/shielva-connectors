"""Notion connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, List, Optional

from models import ConnectorDocument


def _extract_title(properties: Dict[str, Any]) -> str:
    """Extract plain-text title from a Notion page properties dict."""
    # Notion pages always have a 'title' property (key may vary in databases).
    # We check common keys: "title", "Name", "Task name", "Page", etc.
    for key, prop in properties.items():
        if isinstance(prop, dict) and prop.get("type") == "title":
            title_array = prop.get("title", [])
            return "".join(
                rt.get("plain_text", "") for rt in title_array if isinstance(rt, dict)
            )
    return "Untitled"


def _extract_rich_text(rich_text: List[Dict[str, Any]]) -> str:
    """Concatenate plain_text from a Notion rich_text array."""
    return "".join(rt.get("plain_text", "") for rt in rich_text if isinstance(rt, dict))


def _block_to_text(block: Dict[str, Any]) -> str:
    """Convert a single Notion block to plain text."""
    block_type = block.get("type", "")
    block_data = block.get(block_type, {})

    if not isinstance(block_data, dict):
        return ""

    # Code block — handle before generic rich_text path to preserve fencing
    if block_type == "code":
        code_text = _extract_rich_text(block_data.get("rich_text", []))
        language = block_data.get("language", "")
        return f"```{language}\n{code_text}\n```"

    # Divider
    if block_type == "divider":
        return "---"

    # Equation
    if block_type == "equation":
        return block_data.get("expression", "")

    # Most block types have a rich_text array
    rich_text = block_data.get("rich_text", [])
    if rich_text:
        prefix = ""
        if block_type in ("heading_1",):
            prefix = "# "
        elif block_type in ("heading_2",):
            prefix = "## "
        elif block_type in ("heading_3",):
            prefix = "### "
        elif block_type in ("bulleted_list_item",):
            prefix = "- "
        elif block_type in ("numbered_list_item",):
            prefix = "1. "
        elif block_type in ("to_do",):
            checked = block_data.get("checked", False)
            prefix = "[x] " if checked else "[ ] "
        elif block_type in ("quote",):
            prefix = "> "
        elif block_type in ("callout",):
            prefix = "> "
        return prefix + _extract_rich_text(rich_text)

    return ""


def normalize_page(
    page: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    content_blocks: Optional[List[Dict[str, Any]]] = None,
) -> ConnectorDocument:
    """Convert a Notion page object into a ConnectorDocument.

    The stable document id is sha256(page_id)[:16].
    content_blocks, if provided, will be flattened into the document content.
    """
    page_id = page.get("id", "")
    stable_id = hashlib.sha256(page_id.encode()).hexdigest()[:16]

    properties = page.get("properties", {})
    title = _extract_title(properties)

    # Build base content from page metadata
    url = page.get("url", "")
    created_time = page.get("created_time", "")
    last_edited_time = page.get("last_edited_time", "")
    archived = page.get("archived", False)
    parent = page.get("parent", {})
    parent_type = parent.get("type", "")

    content_parts: List[str] = [f"Title: {title}"]
    if url:
        content_parts.append(f"URL: {url}")
    if created_time:
        content_parts.append(f"Created: {created_time}")
    if last_edited_time:
        content_parts.append(f"Last edited: {last_edited_time}")
    if parent_type:
        content_parts.append(f"Parent type: {parent_type}")
    if archived:
        content_parts.append("Archived: true")

    # Append block content
    if content_blocks:
        block_texts: List[str] = []
        for block in content_blocks:
            text = _block_to_text(block)
            if text:
                block_texts.append(text)
        if block_texts:
            content_parts.append("\n--- Content ---")
            content_parts.extend(block_texts)

    metadata: Dict[str, Any] = {
        "page_id": page_id,
        "url": url,
        "created_time": created_time,
        "last_edited_time": last_edited_time,
        "archived": archived,
        "parent_type": parent_type,
        "object_type": "page",
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "source": "notion",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="notion_page",
        metadata=metadata,
    )


def normalize_database(
    database: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Notion database object into a ConnectorDocument.

    The stable document id is sha256(database_id)[:16].
    """
    db_id = database.get("id", "")
    stable_id = hashlib.sha256(db_id.encode()).hexdigest()[:16]

    # Database title is a rich_text array at the top level
    title_array = database.get("title", [])
    title = _extract_rich_text(title_array) or "Untitled Database"

    url = database.get("url", "")
    created_time = database.get("created_time", "")
    last_edited_time = database.get("last_edited_time", "")
    archived = database.get("archived", False)

    # Extract property schema summary
    db_properties = database.get("properties", {})
    property_names = list(db_properties.keys())

    content_parts: List[str] = [f"Database: {title}"]
    if url:
        content_parts.append(f"URL: {url}")
    if created_time:
        content_parts.append(f"Created: {created_time}")
    if last_edited_time:
        content_parts.append(f"Last edited: {last_edited_time}")
    if property_names:
        content_parts.append(f"Properties: {', '.join(property_names)}")
    if archived:
        content_parts.append("Archived: true")

    metadata: Dict[str, Any] = {
        "database_id": db_id,
        "url": url,
        "created_time": created_time,
        "last_edited_time": last_edited_time,
        "archived": archived,
        "property_names": property_names,
        "object_type": "database",
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "source": "notion",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="notion_database",
        metadata=metadata,
    )


async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on NotionAuthError — re-authorizing is required.
    Skips retry on NotionNotFoundError — the resource does not exist.
    """
    from exceptions import NotionAuthError, NotionError, NotionNotFoundError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except NotionAuthError:
            raise
        except NotionNotFoundError:
            raise
        except NotionError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
