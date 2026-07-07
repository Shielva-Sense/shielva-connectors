"""Admin endpoints for the provider category taxonomy + provider →
category mapping. Used by ACP (Integration Builder → Manage) to keep
the catalog organised without redeploying.

All routes live under `/api/v3/catalog/categories` and
`/api/v3/catalog/providers/{provider_key}/category`. Reads are
unauthenticated — the gateway handles JWT — and writes return the
updated resource so callers can stay in sync without a re-fetch.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from integration.services import category_service

logger = structlog.get_logger(__name__)

categories_router = APIRouter(prefix="/api/v3/catalog", tags=["catalog-categories"])


# ── Schemas ──────────────────────────────────────────────────────────


class CategoryCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=80)
    slug: str | None = Field(None, min_length=1, max_length=64)
    description: str = Field("", max_length=400)


class CategoryUpdate(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = Field(None, max_length=400)
    sort_order: int | None = None


class ProviderCategoryAssign(BaseModel):
    category_slug: str = Field(..., min_length=1, max_length=64)


# ── Caller identity (best-effort) ────────────────────────────────────


def _caller(request: Request) -> str | None:
    """Pull the authenticated user from gateway-forwarded headers when
    present. Used only as `updated_by` audit metadata — failure is
    non-fatal."""
    return request.headers.get("x-user-email") or request.headers.get("x-user-id") or None


# ── Categories ───────────────────────────────────────────────────────


@categories_router.get("/categories")
async def list_categories():
    items = await category_service.list_categories()
    return {"categories": items, "count": len(items)}


@categories_router.post("/categories", status_code=201)
async def create_category(body: CategoryCreate):
    try:
        created = await category_service.create_category(body.label, slug=body.slug, description=body.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("categories.created", slug=created["slug"])
    return created


@categories_router.patch("/categories/{slug}")
async def update_category(slug: str, body: CategoryUpdate):
    try:
        updated = await category_service.update_category(
            slug,
            label=body.label,
            description=body.description,
            sort_order=body.sort_order,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("categories.updated", slug=slug)
    return updated


@categories_router.delete("/categories/{slug}")
async def delete_category(slug: str):
    try:
        result = await category_service.delete_category(slug)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info("categories.deleted", slug=slug)
    return result


# ── Provider → category mapping ──────────────────────────────────────


@categories_router.put("/providers/{provider_key}/category")
async def assign_provider_category(provider_key: str, body: ProviderCategoryAssign, request: Request):
    try:
        result = await category_service.set_provider_category(
            provider_key, body.category_slug, updated_by=_caller(request)
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "categories.provider_mapped",
        provider_key=provider_key,
        category_slug=body.category_slug,
    )
    return result
