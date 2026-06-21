"""Normalize a Microsoft Graph OneNote page into a Shielva NormalizedDocument."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

import structlog
from shared.base_connector import NormalizedDocument

from helpers.utils import parse_iso_datetime

logger = structlog.get_logger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    text = _HTML_TAG_RE.sub(" ", html or "")
    return _WS_RE.sub(" ", text).strip()


def normalize_page(
    page: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    content_html: Optional[str] = None,
) -> NormalizedDocument:
    """Convert a Graph OneNote page object (+ optional XHTML content) to a NormalizedDocument.

    The canonical document id is ``f"{tenant_id}_{source_id}"`` so that two
    different Shielva tenants enrolling the same Microsoft account never
    collide in the knowledge base.
    """
    page_id = page.get("id", "")
    title = page.get("title") or "(untitled page)"
    links = page.get("links") or {}
    web_url = (
        (links.get("oneNoteWebUrl") or {}).get("href")
        or page.get("self", "")
    )

    parent_section = page.get("parentSection") or {}
    parent_notebook = page.get("parentNotebook") or {}

    created_at = parse_iso_datetime(page.get("createdDateTime"))
    updated_at = parse_iso_datetime(page.get("lastModifiedDateTime")) or created_at

    content_text = _strip_html(content_html or "") or title

    return NormalizedDocument(
        id=f"{tenant_id}_{page_id}",
        source_id=page_id,
        title=title,
        content=content_text,
        content_type="text",
        source_url=web_url or None,
        url=web_url or None,
        author=page.get("createdByAppId") or None,
        created_at=created_at,
        updated_at=updated_at,
        source="onenote",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "parent_section_id": parent_section.get("id", ""),
            "parent_section_name": parent_section.get("displayName", ""),
            "parent_notebook_id": parent_notebook.get("id", ""),
            "parent_notebook_name": parent_notebook.get("displayName", ""),
            "content_url": page.get("contentUrl", ""),
            "level": page.get("level", 0),
            "order": page.get("order", 0),
            "web_url": web_url or "",
        },
    )
