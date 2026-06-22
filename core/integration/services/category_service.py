"""Provider category service.

DB-backed source of truth for the provider category taxonomy and the
provider → category mapping. The JSON catalog
(`integration/data/connector_catalog.json`) and the Python
`SERVICE_CATALOG` continue to ship category strings, but those are only
treated as **seed values** — once a row exists in MongoDB it wins.

Concretely:
    1. `provider_categories`        — distinct category records (slug + label)
    2. `provider_category_map`      — one row per provider_key with its slug

Both collections are populated by `seed_categories_from_json()` on first
boot and remain idempotent across restarts.

Reads are hot — every `/api/v3/catalog/providers` request resolves
hundreds of providers — so the lookup map is cached in-process with a
short TTL and explicitly invalidated whenever an admin endpoint mutates
the mapping.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog
from pymongo import ASCENDING

from integration.db.database import (
    provider_categories_collection,
    provider_category_map_collection,
)

logger = structlog.get_logger(__name__)

_CATALOG_JSON_PATH = (
    Path(__file__).parent.parent / "data" / "connector_catalog.json"
)

# ── In-process cache ─────────────────────────────────────────────────
# Resolving categories per request is hot path. Cache the mapping in
# memory for a few seconds and invalidate on writes.
_CACHE_TTL_S = 30.0
_cache: dict[str, str] = {}
_cache_expires_at: float = 0.0


def invalidate_cache() -> None:
    """Drop the in-process map cache. Call after any write to the
    provider→category mapping or to the category taxonomy."""
    global _cache_expires_at
    _cache_expires_at = 0.0


# ── Slug helpers ─────────────────────────────────────────────────────

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(label: str) -> str:
    """Convert a freeform category label to a stable slug.

    "CRM & Sales"            → "crm-sales"
    "Government APIs (India)" → "government-apis-india"
    """
    s = _SLUG_STRIP.sub("-", label.lower()).strip("-")
    return s or "uncategorized"


# ── Indexes ──────────────────────────────────────────────────────────


async def ensure_indexes() -> None:
    """Create the indexes the read + write paths rely on. Idempotent."""
    try:
        await provider_categories_collection().create_index(
            [("slug", ASCENDING)], unique=True, name="slug_unique"
        )
        await provider_categories_collection().create_index(
            [("sort_order", ASCENDING)], name="sort_order"
        )
        await provider_category_map_collection().create_index(
            [("provider_key", ASCENDING)], unique=True, name="provider_key_unique"
        )
        await provider_category_map_collection().create_index(
            [("category_slug", ASCENDING)], name="category_slug"
        )
    except Exception as exc:
        logger.warning("categories.ensure_indexes_failed", error=str(exc))


# ── Seed from JSON ───────────────────────────────────────────────────


async def seed_categories_from_json() -> dict:
    """One-shot seed of the categories + mapping from
    `connector_catalog.json`. Idempotent — only inserts rows that are
    missing, never overwrites human edits."""
    try:
        with open(_CATALOG_JSON_PATH) as fh:
            raw = json.load(fh)
    except Exception as exc:
        logger.warning("categories.seed_load_failed", error=str(exc))
        return {"inserted_categories": 0, "inserted_mappings": 0}

    providers = raw if isinstance(raw, list) else raw.get("providers", [])

    # ── 1. Seed the taxonomy from distinct category labels ───────────
    labels: dict[str, str] = {}  # slug → label (first wins)
    for p in providers:
        label = (p.get("category") or "").strip()
        if not label:
            continue
        slug = slugify(label)
        labels.setdefault(slug, label)

    cats_coll = provider_categories_collection()
    inserted_cats = 0
    now = datetime.now(timezone.utc)
    for sort_order, slug in enumerate(sorted(labels)):
        # Upsert-if-missing: do NOT overwrite an existing row (an admin
        # may have renamed the label).
        existing = await cats_coll.find_one({"slug": slug})
        if existing:
            continue
        await cats_coll.insert_one(
            {
                "slug": slug,
                "label": labels[slug],
                "description": "",
                "sort_order": sort_order,
                "source": "seed",
                "created_at": now,
                "updated_at": now,
            }
        )
        inserted_cats += 1

    # ── 2. Seed the provider → category mapping ──────────────────────
    map_coll = provider_category_map_collection()
    inserted_maps = 0
    for p in providers:
        provider_key = (p.get("key") or p.get("provider") or "").strip()
        label = (p.get("category") or "").strip()
        if not provider_key or not label:
            continue
        # Only insert if missing — never clobber a manual remap.
        existing = await map_coll.find_one({"provider_key": provider_key})
        if existing:
            continue
        await map_coll.insert_one(
            {
                "provider_key": provider_key,
                "category_slug": slugify(label),
                "source": "seed",
                "updated_at": now,
                "updated_by": None,
            }
        )
        inserted_maps += 1

    if inserted_cats or inserted_maps:
        invalidate_cache()
        logger.info(
            "categories.seeded",
            inserted_categories=inserted_cats,
            inserted_mappings=inserted_maps,
            total_categories=len(labels),
            total_providers=len(providers),
        )
    return {
        "inserted_categories": inserted_cats,
        "inserted_mappings": inserted_maps,
    }


# ── Read path ────────────────────────────────────────────────────────


async def get_provider_category_map() -> dict[str, str]:
    """Return `{provider_key: category_label}` for every mapped provider.

    Cached in-process for `_CACHE_TTL_S` seconds. Called on every
    catalog list request, so the cache is non-optional.
    """
    global _cache, _cache_expires_at
    now = time.monotonic()
    if _cache and now < _cache_expires_at:
        return _cache

    # Build slug → label first, then provider_key → label.
    slug_to_label: dict[str, str] = {}
    async for cat in provider_categories_collection().find(
        {}, {"slug": 1, "label": 1}
    ):
        slug_to_label[cat["slug"]] = cat.get("label", cat["slug"])

    out: dict[str, str] = {}
    async for row in provider_category_map_collection().find(
        {}, {"provider_key": 1, "category_slug": 1}
    ):
        key = row.get("provider_key")
        slug = row.get("category_slug")
        if not key or not slug:
            continue
        label = slug_to_label.get(slug)
        if label:
            out[key] = label

    _cache = out
    _cache_expires_at = now + _CACHE_TTL_S
    return out


# ── Admin helpers ────────────────────────────────────────────────────


async def list_categories() -> list[dict]:
    """List all categories with their current provider count."""
    cats = await provider_categories_collection().find({}).to_list(None)
    counts: dict[str, int] = {}
    pipeline = [
        {"$group": {"_id": "$category_slug", "n": {"$sum": 1}}},
    ]
    async for row in provider_category_map_collection().aggregate(pipeline):
        counts[row["_id"]] = row["n"]

    out = []
    for c in cats:
        out.append(
            {
                "slug": c["slug"],
                "label": c.get("label", c["slug"]),
                "description": c.get("description", ""),
                "sort_order": c.get("sort_order", 0),
                "source": c.get("source", "seed"),
                "provider_count": counts.get(c["slug"], 0),
            }
        )
    out.sort(key=lambda r: (r["sort_order"], r["label"].lower()))
    return out


async def create_category(
    label: str, *, slug: Optional[str] = None, description: str = ""
) -> dict:
    """Create a new category. Slug is auto-derived from the label when
    omitted. Raises ValueError on duplicate slug."""
    label = (label or "").strip()
    if not label:
        raise ValueError("label is required")
    final_slug = (slug or slugify(label)).strip()
    if not final_slug:
        raise ValueError("slug must be non-empty")

    coll = provider_categories_collection()
    if await coll.find_one({"slug": final_slug}):
        raise ValueError(f"category '{final_slug}' already exists")

    # Place at end of sort order
    last = await coll.find({}, {"sort_order": 1}).sort("sort_order", -1).limit(1).to_list(1)
    next_sort = (last[0].get("sort_order", 0) + 1) if last else 0

    now = datetime.now(timezone.utc)
    doc = {
        "slug": final_slug,
        "label": label,
        "description": description,
        "sort_order": next_sort,
        "source": "user",
        "created_at": now,
        "updated_at": now,
    }
    await coll.insert_one(doc)
    invalidate_cache()
    return {
        "slug": final_slug,
        "label": label,
        "description": description,
        "sort_order": next_sort,
        "source": "user",
        "provider_count": 0,
    }


async def update_category(
    slug: str,
    *,
    label: Optional[str] = None,
    description: Optional[str] = None,
    sort_order: Optional[int] = None,
) -> dict:
    """Rename / re-describe / re-order a category. Slug is immutable —
    renaming the label preserves all existing mappings."""
    coll = provider_categories_collection()
    existing = await coll.find_one({"slug": slug})
    if not existing:
        raise LookupError(f"category '{slug}' not found")

    patch: dict = {"updated_at": datetime.now(timezone.utc)}
    if label is not None:
        label = label.strip()
        if not label:
            raise ValueError("label cannot be empty")
        patch["label"] = label
    if description is not None:
        patch["description"] = description
    if sort_order is not None:
        patch["sort_order"] = int(sort_order)

    await coll.update_one({"slug": slug}, {"$set": patch})
    invalidate_cache()
    merged = {**existing, **patch}
    return {
        "slug": slug,
        "label": merged.get("label", slug),
        "description": merged.get("description", ""),
        "sort_order": merged.get("sort_order", 0),
        "source": merged.get("source", "seed"),
    }


async def delete_category(slug: str) -> dict:
    """Delete a category. Refuses to delete if any provider is still
    mapped to it — the caller must remap or accept reassignment first."""
    coll = provider_categories_collection()
    existing = await coll.find_one({"slug": slug})
    if not existing:
        raise LookupError(f"category '{slug}' not found")

    in_use = await provider_category_map_collection().count_documents(
        {"category_slug": slug}, limit=1
    )
    if in_use:
        raise ValueError(
            f"category '{slug}' has providers mapped to it — remap them first"
        )

    await coll.delete_one({"slug": slug})
    invalidate_cache()
    return {"slug": slug, "deleted": True}


async def set_provider_category(
    provider_key: str, category_slug: str, *, updated_by: Optional[str] = None
) -> dict:
    """Map (or remap) a provider to a category.

    The category lookup is lenient — callers may pass either a stored slug
    or a freeform label. If the value doesn't match an existing slug, we
    look it up by label, and as a last resort auto-create the row so
    every label used in the catalog ends up addressable from the UI.
    """
    provider_key = (provider_key or "").strip()
    category_slug = (category_slug or "").strip()
    if not provider_key:
        raise ValueError("provider_key is required")
    if not category_slug:
        raise ValueError("category_slug is required")

    coll = provider_categories_collection()
    cat = await coll.find_one({"slug": category_slug})

    # Fallback 1: maybe the caller passed a label that doesn't yet have a
    # row but matches one by label (case-insensitive).
    if not cat:
        cat = await coll.find_one(
            {"label": {"$regex": f"^{category_slug}$", "$options": "i"}}
        )

    # Fallback 2: auto-create. Slugify the input, treat the original as
    # the label. This makes every freeform label from the static catalog
    # addressable without an extra admin step.
    if not cat:
        new_slug = slugify(category_slug) or category_slug.lower()
        existing = await coll.find_one({"slug": new_slug})
        if existing:
            cat = existing
        else:
            last = (
                await coll.find({}, {"sort_order": 1})
                .sort("sort_order", -1)
                .limit(1)
                .to_list(1)
            )
            next_sort = (last[0].get("sort_order", 0) + 1) if last else 0
            now = datetime.now(timezone.utc)
            doc = {
                "slug": new_slug,
                "label": category_slug,
                "description": "",
                "sort_order": next_sort,
                "source": "user",
                "created_at": now,
                "updated_at": now,
            }
            await coll.insert_one(doc)
            cat = doc

    final_slug = cat["slug"]

    now = datetime.now(timezone.utc)
    await provider_category_map_collection().update_one(
        {"provider_key": provider_key},
        {
            "$set": {
                "category_slug": final_slug,
                "source": "override",
                "updated_at": now,
                "updated_by": updated_by,
            }
        },
        upsert=True,
    )
    invalidate_cache()
    return {
        "provider_key": provider_key,
        "category_slug": final_slug,
        "category_label": cat.get("label", final_slug),
        "source": "override",
    }
