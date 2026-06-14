"""Integration Builder — Documentation Builder API routes.

Provides endpoints for generating, updating, retrieving, and exporting
connector documentation as structured JSON (rendered by SiteRenderer).
"""

import asyncio
import json
import structlog
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional

from bson import ObjectId
from datetime import datetime

from integration.db.database import sessions_collection
from integration.services.docs_builder_service import (
    export_docs_html,
    generate_docs,
    update_docs_with_prompt,
)
from integration.services import r2_service

logger = structlog.get_logger(__name__)

docs_router = APIRouter(prefix="/sessions", tags=["docs"])


# ── Request/Response models ──────────────────────────────────────────

class GenerateDocsRequest(BaseModel):
    extra_prompt: Optional[str] = ""


class UpdateDocsRequest(BaseModel):
    prompt: str
    current_json: Dict[str, Any]


class SaveDocsRequest(BaseModel):
    docs: Dict[str, Any]


@docs_router.post("/{session_id}/docs/save")
async def save_session_docs(
    session_id: str,
    body: SaveDocsRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Persist client-supplied docs JSON (e.g. regenerated locally in SAD) to BOTH
    Mongo ``session.docs_json`` AND R2 ``CONNECTOR_DOCS``.

    The docs reader (``GET /{id}/docs``) prefers R2 and falls back to Mongo, so
    writing both makes a SAD-side regenerate show up immediately in ACP — the
    previously-missing bridge between local docs and the store ACP reads.
    """
    docs_json = body.docs
    if not isinstance(docs_json, dict) or not docs_json.get("sections"):
        raise HTTPException(status_code=400, detail="docs must be a JSON object with a non-empty 'sections' array")
    try:
        oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    now = datetime.utcnow()
    result = await sessions_collection().update_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"$set": {"docs_json": docs_json, "docs_updated_at": now}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Session not found")

    # Also persist to R2 (the reader prefers R2 when it has sections).
    session_meta = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"provider": 1, "service_slug": 1},
    )
    if session_meta and session_meta.get("provider") and session_meta.get("service_slug"):
        try:
            await r2_service.save_connector_docs(
                tenant_id=x_tenant_id,
                provider=session_meta.get("provider", ""),
                service_slug=session_meta.get("service_slug", ""),
                docs=docs_json,
            )
        except Exception as exc:
            # Mongo is updated regardless; R2 is best-effort (and the reader falls back to Mongo).
            logger.warning("docs_routes.save_r2_failed", session_id=session_id, error=str(exc)[:200])

    logger.info("docs_routes.saved", session_id=session_id, tenant_id=x_tenant_id, sections=len(docs_json.get("sections", [])))
    return {"saved": True, "updated_at": str(now)}


class DocsSection(BaseModel):
    id: str
    title: str
    content: str
    children: Optional[List[Dict[str, Any]]] = None


class DocsResponse(BaseModel):
    title: str
    sections: List[Dict[str, Any]]


# ── Routes ───────────────────────────────────────────────────────────

@docs_router.post("/{session_id}/docs/generate")
async def generate_session_docs(
    session_id: str,
    body: GenerateDocsRequest = GenerateDocsRequest(),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Generate connector documentation as SiteRenderer JSON.

    Reads the generated connector files and uses the LLM to produce
    structured documentation. The result is saved to the session and returned.
    """
    logger.info("docs_routes.generate", session_id=session_id, tenant_id=x_tenant_id)
    try:
        docs_json = await generate_docs(
            session_id=session_id,
            tenant_id=x_tenant_id,
            extra_prompt=body.extra_prompt or "",
        )
        # Persist to R2 for connector-level durability (survives session deletion/expiry)
        session_meta = await sessions_collection().find_one(
            {"_id": ObjectId(session_id), "tenant_id": x_tenant_id},
            {"provider": 1, "service_slug": 1},
        )
        if session_meta:
            await r2_service.save_connector_docs(
                tenant_id=x_tenant_id,
                provider=session_meta.get("provider", ""),
                service_slug=session_meta.get("service_slug", ""),
                docs=docs_json,
            )
        return docs_json
    except ValueError as exc:
        logger.warning("docs_routes.generate_validation_error", session_id=session_id, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("docs_routes.generate_failed", session_id=session_id, error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Documentation generation failed: {exc}")


@docs_router.get("/{session_id}/docs/generate-stream")
async def generate_session_docs_stream(
    session_id: str,
    extra_prompt: str = "",
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """SSE endpoint — streams Gemini logs then sends a 'complete' event with the docs JSON."""
    queue: asyncio.Queue = asyncio.Queue()

    async def log_cb(level: str, message: str):
        await queue.put({"type": "log", "level": level, "message": message})

    async def run():
        try:
            docs_json = await generate_docs(
                session_id=session_id,
                tenant_id=x_tenant_id,
                extra_prompt=extra_prompt or "",
                log_cb=log_cb,
            )
            # Persist to R2
            session_meta = await sessions_collection().find_one(
                {"_id": ObjectId(session_id), "tenant_id": x_tenant_id},
                {"provider": 1, "service_slug": 1},
            )
            if session_meta:
                await r2_service.save_connector_docs(
                    tenant_id=x_tenant_id,
                    provider=session_meta.get("provider", ""),
                    service_slug=session_meta.get("service_slug", ""),
                    docs=docs_json,
                )
            await queue.put({"type": "complete", "docs": docs_json})
        except Exception as exc:
            await queue.put({"type": "error", "message": str(exc)})
        finally:
            await queue.put(None)  # sentinel

    async def event_generator():
        asyncio.create_task(run())
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@docs_router.post("/{session_id}/docs/update")
async def update_session_docs(
    session_id: str,
    body: UpdateDocsRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Update existing documentation JSON based on a user prompt.

    Takes the current docs JSON and a user instruction, uses the LLM
    to produce an updated version, saves it, and returns the result.
    """
    logger.info("docs_routes.update", session_id=session_id, tenant_id=x_tenant_id)
    try:
        updated_json = await update_docs_with_prompt(
            session_id=session_id,
            tenant_id=x_tenant_id,
            prompt=body.prompt,
            current_json=body.current_json,
        )
        # Update R2 with the new version after every prompt-driven update
        session_meta = await sessions_collection().find_one(
            {"_id": ObjectId(session_id), "tenant_id": x_tenant_id},
            {"provider": 1, "service_slug": 1},
        )
        if session_meta:
            await r2_service.save_connector_docs(
                tenant_id=x_tenant_id,
                provider=session_meta.get("provider", ""),
                service_slug=session_meta.get("service_slug", ""),
                docs=updated_json,
            )
        return updated_json
    except ValueError as exc:
        logger.warning("docs_routes.update_validation_error", session_id=session_id, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("docs_routes.update_failed", session_id=session_id, error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Documentation update failed: {exc}")


@docs_router.get("/{session_id}/docs/export-html")
async def export_session_docs_html(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Export documentation as a standalone HTML page.

    Reads the stored docs_json from the session and converts it to
    a self-contained HTML document with sidebar navigation and inline CSS.
    """
    logger.info("docs_routes.export_html", session_id=session_id, tenant_id=x_tenant_id)

    try:
        oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"docs_json": 1},
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    docs_json = session.get("docs_json")
    if not docs_json:
        raise HTTPException(status_code=404, detail="No documentation found for this session. Generate docs first.")

    try:
        html = await export_docs_html(docs_json)
        return HTMLResponse(content=html, status_code=200)
    except Exception as exc:
        logger.error("docs_routes.export_html_failed", session_id=session_id, error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail=f"HTML export failed: {exc}")


@docs_router.get("/{session_id}/docs")
async def get_session_docs(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Return the stored docs_json from the session.

    Returns the most recently generated/updated documentation JSON,
    or 404 if documentation has not been generated yet.
    """
    logger.info("docs_routes.get", session_id=session_id, tenant_id=x_tenant_id)

    try:
        oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"docs_json": 1, "docs_generated_at": 1, "docs_updated_at": 1, "doc_prompts": 1,
         "provider": 1, "service_slug": 1},
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    docs_json = session.get("docs_json")

    # Try R2 first — it may have a newer version (e.g. from regenerate)
    provider = session.get("provider", "")
    service_slug = session.get("service_slug", "")
    if provider and service_slug:
        r2_docs = await r2_service.get_connector_docs(x_tenant_id, provider, service_slug)
        if r2_docs and r2_docs.get("sections"):
            docs_json = r2_docs  # R2 is source of truth

    if not docs_json:
        raise HTTPException(status_code=404, detail="No documentation found for this session. Generate docs first.")

    return {
        "docs": docs_json,
        "generated_at": str(session.get("docs_generated_at", "")),
        "updated_at": str(session.get("docs_updated_at", "")),
        "doc_prompts": session.get("doc_prompts", []),
    }


@docs_router.put("/{session_id}/docs/prompts")
async def save_doc_prompts(
    session_id: str,
    payload: dict,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Persist the list of doc prompt instructions for this session."""
    try:
        oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    prompts = payload.get("prompts", [])
    if not isinstance(prompts, list):
        raise HTTPException(status_code=422, detail="prompts must be a list")

    result = await sessions_collection().update_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"$set": {"doc_prompts": prompts}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"saved": True, "count": len(prompts)}


# ── Code Analysis routes ─────────────────────────────────────────────

@docs_router.post("/{session_id}/code-analysis")
async def generate_session_code_analysis(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Use Gemini to analyse connector.py and return annotated sections + sequence diagram."""
    logger.info("docs_routes.code_analysis_generate", session_id=session_id, tenant_id=x_tenant_id)
    try:
        from integration.services.code_analysis_service import generate_code_analysis
        result = await generate_code_analysis(session_id=session_id, tenant_id=x_tenant_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("docs_routes.code_analysis_failed", session_id=session_id, error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Code analysis failed: {exc}")


@docs_router.get("/{session_id}/code-analysis")
async def get_session_code_analysis(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Return previously generated code analysis, or 404 if not generated yet."""
    try:
        from integration.services.code_analysis_service import get_code_analysis
        result = await get_code_analysis(session_id=session_id, tenant_id=x_tenant_id)
        if not result:
            raise HTTPException(status_code=404, detail="Code analysis not generated yet")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@docs_router.delete("/{session_id}/code-analysis")
async def delete_session_code_analysis(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Delete persisted code analysis from R2 for this session.

    Called on re-execute and cleanup so the Code Explorer re-analyses
    the freshly generated connector instead of serving stale data.
    """
    logger.info("docs_routes.code_analysis_delete", session_id=session_id, tenant_id=x_tenant_id)
    try:
        from integration.services.code_analysis_service import delete_code_analysis
        deleted = await delete_code_analysis(session_id=session_id, tenant_id=x_tenant_id)
        return {"deleted": deleted}
    except Exception as exc:
        logger.warning("docs_routes.code_analysis_delete_failed", session_id=session_id, error=str(exc))
        # Non-fatal — return ok=False rather than 500
        return {"deleted": False, "error": str(exc)}
