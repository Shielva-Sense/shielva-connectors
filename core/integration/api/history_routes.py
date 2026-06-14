"""Integration Builder — Plan history API routes (R2-backed)."""

from typing import Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

from integration.services import r2_service

logger = structlog.get_logger(__name__)

history_router = APIRouter(tags=["history"])


def _get_tenant(x_tenant_id: Optional[str] = Header(None)) -> str:
    if not x_tenant_id:
        raise HTTPException(400, "X-Tenant-ID header is required")
    return x_tenant_id


@history_router.get("/catalog/{provider}/{service}/history")
async def get_service_history(
    provider: str,
    service: str,
    x_tenant_id: Optional[str] = Header(None),
    service_slug: Optional[str] = Query(None, description="Connector slug (derived from connector name). Defaults to service name if not provided."),
):
    """Return the latest prompt, cached plan JSON, and plan markdown from R2.

    Returns { has_history: false } when no history exists or R2 is unconfigured.
    Returns { has_history: true, latest_prompt, date_executed, plan, plan_markdown } otherwise.
    """
    tenant_id = _get_tenant(x_tenant_id)
    slug = service_slug or service.replace("-", "_").lower()
    try:
        history = await r2_service.get_history(provider, slug, tenant_id)
        if not history:
            return {"has_history": False}
        return history
    except Exception as exc:
        logger.warning(
            "history.fetch_failed",
            provider=provider,
            service_slug=slug,
            tenant_id=tenant_id,
            error=str(exc),
        )
        return {"has_history": False}


@history_router.get(
    "/catalog/{provider}/{service}/plan.md",
    response_class=PlainTextResponse,
    responses={200: {"content": {"text/markdown": {}}}},
)
async def get_plan_markdown(
    provider: str,
    service: str,
    x_tenant_id: Optional[str] = Header(None),
    service_slug: Optional[str] = Query(None, description="Connector slug (derived from connector name). Defaults to service name if not provided."),
):
    """Return the latest plan.md for a provider/service directly from R2.

    Useful for previewing or downloading the plan without calling the LLM.
    """
    tenant_id = _get_tenant(x_tenant_id)
    slug = service_slug or service.replace("-", "_").lower()
    md = await r2_service.get_plan_markdown(provider, slug, tenant_id)
    if not md:
        raise HTTPException(404, f"No plan.md found for {provider}/{slug}")
    return PlainTextResponse(content=md, media_type="text/markdown")
