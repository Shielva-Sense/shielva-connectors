"""Transforms raw Document360 API responses into NormalizedDocument objects.

NormalizedDocument `id` is `f"{tenant_id}_{source_id}"` per the connector spec
— tenant-scoped, never connector-scoped, so cross-tenant collisions are
impossible even when two tenants reference the same upstream article id.
"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import structlog
from shared.base_connector import NormalizedDocument

from helpers.utils import build_article_url, extract_id

logger = structlog.get_logger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    return _HTML_TAG_RE.sub("", html).strip()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        # Document360 returns ISO 8601 with optional 'Z' suffix
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def normalize_article(
    article: Dict[str, Any],
    tenant_id: str,
    connector_id: str,
    project_slug: Optional[str] = None,
) -> NormalizedDocument:
    """Convert a Document360 article object into a NormalizedDocument.

    Tolerates camelCase and PascalCase field names from the Document360 API.
    The NormalizedDocument id is `tenant_id_source_id` (mandatory format).
    """
    article_id = extract_id(article, "id", "Id", "articleId", "ArticleId")
    title = article.get("title") or article.get("Title") or "(untitled)"
    raw_content = (
        article.get("content")
        or article.get("Content")
        or article.get("htmlContent")
        or article.get("HtmlContent")
        or ""
    )
    content_type = "html" if ("<" in raw_content and ">" in raw_content) else "text"
    plain_content = _strip_html(raw_content) if content_type == "html" else raw_content

    language_code = (
        article.get("languageCode")
        or article.get("LanguageCode")
        or article.get("language")
        or "en"
    )
    category_id = extract_id(article, "categoryId", "CategoryId", default="")

    created_at = _parse_iso(
        article.get("createdAt")
        or article.get("CreatedAt")
        or article.get("created_at")
    )
    updated_at = _parse_iso(
        article.get("modifiedAt")
        or article.get("ModifiedAt")
        or article.get("updatedAt")
        or article.get("UpdatedAt")
    )

    author = (
        article.get("author")
        or article.get("Author")
        or article.get("createdBy")
        or article.get("CreatedBy")
        or ""
    )

    return NormalizedDocument(
        id=f"{tenant_id}_{article_id}",
        source_id=article_id,
        title=title,
        content=plain_content,
        content_type=content_type,
        source_url=build_article_url(article_id, project_slug, language_code),
        author=author if isinstance(author, str) else "",
        created_at=created_at,
        updated_at=updated_at or created_at,
        source="document360",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "category_id": category_id,
            "language_code": language_code,
            "is_published": bool(
                article.get("isPublished")
                or article.get("IsPublished")
                or article.get("published")
            ),
            "raw_content_type": content_type,
            "raw_html": raw_content if content_type == "html" else "",
        },
    )
