from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ProductboardAuthError, ProductboardError, ProductboardRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _short_id(value: str) -> str:
    """Return a 16-character hex digest (sha256 prefix) for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_feature(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Productboard feature into a ConnectorDocument.

    Stable source_id = sha256("feature:" + id)[:16].
    """
    feature_id: str = raw.get("id", "") or ""
    name: str = raw.get("name", "") or f"Feature {feature_id}"
    description: str = raw.get("description", "") or ""
    status_raw = raw.get("status", {}) or {}
    status: str = (
        status_raw.get("name", "") if isinstance(status_raw, dict) else str(status_raw)
    ) or ""
    created_at: str = raw.get("createdAt", "") or ""
    updated_at: str = raw.get("updatedAt", "") or ""

    # Parent linkage
    parent = raw.get("parent", {}) or {}
    parent_id: str = parent.get("id", "") if isinstance(parent, dict) else ""

    # Owner
    owner = raw.get("owner", {}) or {}
    owner_email: str = owner.get("email", "") if isinstance(owner, dict) else ""

    content_parts: list[str] = [f"Feature: {name}"]
    if description:
        content_parts.append(f"Description:\n{description}")
    if status:
        content_parts.append(f"Status: {status}")
    if owner_email:
        content_parts.append(f"Owner: {owner_email}")

    source_id = _short_id(f"feature:{feature_id}") if feature_id else _short_id(name)
    source_url = (
        f"https://app.productboard.com/features/{feature_id}" if feature_id else ""
    )

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "feature_id": feature_id,
            "status": status,
            "parent_id": parent_id,
            "owner_email": owner_email,
            "created_at": created_at,
            "updated_at": updated_at,
            "resource_type": "feature",
        },
    )


def normalize_component(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Productboard component into a ConnectorDocument.

    Stable source_id = sha256("component:" + id)[:16].
    """
    component_id: str = raw.get("id", "") or ""
    name: str = raw.get("name", "") or f"Component {component_id}"
    description: str = raw.get("description", "") or ""
    created_at: str = raw.get("createdAt", "") or ""
    updated_at: str = raw.get("updatedAt", "") or ""

    # Parent product linkage
    product = raw.get("product", {}) or {}
    product_id: str = product.get("id", "") if isinstance(product, dict) else ""
    product_name: str = product.get("name", "") if isinstance(product, dict) else ""

    content_parts: list[str] = [f"Component: {name}"]
    if description:
        content_parts.append(f"Description:\n{description}")
    if product_name:
        content_parts.append(f"Product: {product_name}")

    source_id = (
        _short_id(f"component:{component_id}") if component_id else _short_id(name)
    )
    source_url = (
        f"https://app.productboard.com/components/{component_id}"
        if component_id
        else ""
    )

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "component_id": component_id,
            "product_id": product_id,
            "product_name": product_name,
            "created_at": created_at,
            "updated_at": updated_at,
            "resource_type": "component",
        },
    )


def normalize_note(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Productboard note into a ConnectorDocument.

    Stable source_id = sha256("note:" + id)[:16].
    """
    note_id: str = raw.get("id", "") or ""
    title: str = raw.get("title", "") or f"Note {note_id}"
    content_body: str = raw.get("content", "") or ""
    created_at: str = raw.get("createdAt", "") or ""
    updated_at: str = raw.get("updatedAt", "") or ""

    # Author
    author = raw.get("author", {}) or {}
    author_email: str = author.get("email", "") if isinstance(author, dict) else ""

    # Feature linkage
    feature_ref = raw.get("feature", {}) or {}
    feature_id: str = (
        feature_ref.get("id", "") if isinstance(feature_ref, dict) else ""
    )

    content_parts: list[str] = [f"Note: {title}"]
    if content_body:
        content_parts.append(f"Content:\n{content_body}")
    if author_email:
        content_parts.append(f"Author: {author_email}")
    if feature_id:
        content_parts.append(f"Feature ID: {feature_id}")

    source_id = _short_id(f"note:{note_id}") if note_id else _short_id(title)
    source_url = (
        f"https://app.productboard.com/notes/{note_id}" if note_id else ""
    )

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "note_id": note_id,
            "author_email": author_email,
            "feature_id": feature_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "resource_type": "note",
        },
    )


def normalize_product(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Productboard product into a ConnectorDocument.

    Stable source_id = sha256("product:" + id)[:16].
    """
    product_id: str = raw.get("id", "") or ""
    name: str = raw.get("name", "") or f"Product {product_id}"
    description: str = raw.get("description", "") or ""
    created_at: str = raw.get("createdAt", "") or ""
    updated_at: str = raw.get("updatedAt", "") or ""

    content_parts: list[str] = [f"Product: {name}"]
    if description:
        content_parts.append(f"Description:\n{description}")

    source_id = (
        _short_id(f"product:{product_id}") if product_id else _short_id(name)
    )
    source_url = (
        f"https://app.productboard.com/products/{product_id}" if product_id else ""
    )

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "product_id": product_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "resource_type": "product",
        },
    )


# ── Retry helper ──────────────────────────────────────────────────────────────


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
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: ProductboardError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ProductboardAuthError:
            raise  # no retry on auth failures
        except ProductboardRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ProductboardError as exc:
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
