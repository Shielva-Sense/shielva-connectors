"""Integration Builder — Documentation Builder API routes.

Provides endpoints for generating, updating, retrieving, and exporting
connector documentation as structured JSON (rendered by SiteRenderer).
"""

import asyncio
import json
import re as _re
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from integration.core.config import settings
from integration.db.database import sessions_collection
from integration.services import r2_service
from integration.services.docs_builder_service import (
    export_docs_html,
    generate_docs,
    update_docs_with_prompt,
)

logger = structlog.get_logger(__name__)

_SHIELVA_DOCS_REL = Path(".shielva") / "docs" / "connector_docs.json"


def _read_local_docs(tenant_id: str, service_slug: str, provider: str = "") -> dict | None:
    """Read connector_docs.json from generated_connectors/ (local fallback when R2 not configured).

    Scans all subdirectories under {GENERATED_CODE_DIR}/{tenant_id}/ for a
    connector_docs.json, matching by connector_type in metadata/connector.json or
    by a normalized directory-name comparison against service_slug.

    Sessions store the canonical CATALOG service key (e.g. ``analytics_data``)
    while the connector dir's ``connector_type`` is typically the LLM-built
    name (e.g. ``google_analytics``). So we also try the provider-prefixed form
    ``<provider>_<service_slug>`` when computing match candidates.
    """
    if not tenant_id:
        return None
    try:
        base = Path(settings.GENERATED_CODE_DIR).resolve()
        tenant_dir = base / tenant_id
        if not tenant_dir.exists():
            return None

        # Derive candidate slugs:
        #   - bare canonical service_slug (e.g. "analytics_data")
        #   - provider-prefixed form (e.g. "google_analytics_data" → "google_analytics")
        bare = _re.sub(r"_[a-f0-9]{6}$", "", service_slug or "")
        bare = _re.sub(r"_connector$", "", bare).strip("_")
        prov = (provider or "").lower().strip()
        candidates = {bare}
        if prov:
            candidates.add(f"{prov}_{bare}")
        candidates.discard("")

        def _matches(value: str) -> bool:
            v = value.lower()
            v_collapsed = v.replace("_", "")
            for c in candidates:
                if not c:
                    continue
                if v == c or v_collapsed == c.replace("_", ""):
                    return True
                if v.startswith(c) or c.startswith(v):
                    return True
            return False

        for pkg_dir in tenant_dir.iterdir():
            if not pkg_dir.is_dir():
                continue
            docs_path = pkg_dir / _SHIELVA_DOCS_REL
            if not docs_path.exists():
                continue

            matched = False
            # Primary: match via metadata/connector.json connector_type or service field
            meta_path = pkg_dir / "metadata" / "connector.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    ct = meta.get("connector_type") or meta.get("service") or ""
                    if ct and _matches(ct):
                        matched = True
                except Exception:
                    pass

            # Fallback: normalize directory name and compare
            if not matched:
                dir_slug = _re.sub(r"[^a-z0-9]+", "_", pkg_dir.name.lower()).strip("_")
                dir_slug = _re.sub(r"_connector$", "", dir_slug)
                if dir_slug and _matches(dir_slug):
                    matched = True

            if matched:
                try:
                    data = json.loads(docs_path.read_text(encoding="utf-8"))
                    if data.get("sections"):
                        return data
                except Exception:
                    pass
    except Exception:
        pass
    return None


docs_router = APIRouter(prefix="/sessions", tags=["docs"])


# ── Request/Response models ──────────────────────────────────────────


class GenerateDocsRequest(BaseModel):
    extra_prompt: str | None = ""


class UpdateDocsRequest(BaseModel):
    prompt: str
    current_json: dict[str, Any]


class SaveDocsRequest(BaseModel):
    docs: dict[str, Any]


@docs_router.post("/{session_id}/docs/save")
async def save_session_docs(
    session_id: str,
    body: SaveDocsRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Persist client-supplied docs JSON to R2 (primary store, no MongoDB)."""
    docs_json = body.docs
    if not isinstance(docs_json, dict) or not docs_json.get("sections"):
        raise HTTPException(
            status_code=400,
            detail="docs must be a JSON object with a non-empty 'sections' array",
        )
    try:
        oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    session_meta = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"provider": 1, "service_slug": 1, "output_dir": 1},
    )
    if not session_meta:
        raise HTTPException(status_code=404, detail="Session not found")

    now = datetime.utcnow()
    await r2_service.save_connector_docs(
        tenant_id=x_tenant_id,
        provider=session_meta.get("provider", ""),
        service_slug=session_meta.get("service_slug", ""),
        docs=docs_json,
    )

    logger.info(
        "docs_routes.saved",
        session_id=session_id,
        tenant_id=x_tenant_id,
        sections=len(docs_json.get("sections", [])),
    )
    return {"saved": True, "updated_at": str(now)}


class DocsSection(BaseModel):
    id: str
    title: str
    content: str
    children: list[dict[str, Any]] | None = None


class DocsResponse(BaseModel):
    title: str
    sections: list[dict[str, Any]]


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
        logger.warning(
            "docs_routes.generate_validation_error",
            session_id=session_id,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(
            "docs_routes.generate_failed",
            session_id=session_id,
            error=str(exc),
            exc_info=True,
        )
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

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        session_meta = await sessions_collection().find_one(
            {"_id": ObjectId(session_id), "tenant_id": x_tenant_id},
            {"provider": 1, "service_slug": 1, "output_dir": 1},
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
        logger.error(
            "docs_routes.update_failed",
            session_id=session_id,
            error=str(exc),
            exc_info=True,
        )
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

    # docs_json no longer lives in Mongo — pull it from R2 (durable, compressed
    # primary store) via the same loader the modal uses. Fall back to the local
    # `.shielva/docs/connector_docs.json` for dev environments without R2.
    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"provider": 1, "service_slug": 1},
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    provider = session.get("provider", "")
    service_slug = session.get("service_slug", "")
    docs_json = None
    if provider and service_slug:
        docs_json = await r2_service.get_connector_docs(x_tenant_id, provider, service_slug)
        if docs_json and not docs_json.get("sections"):
            docs_json = None
    if not docs_json:
        docs_json = _read_local_docs(x_tenant_id, service_slug, provider)
    if not docs_json:
        raise HTTPException(
            status_code=404,
            detail="No documentation found for this session. Generate docs first.",
        )

    try:
        html = await export_docs_html(docs_json)
        return HTMLResponse(content=html, status_code=200)
    except Exception as exc:
        logger.error(
            "docs_routes.export_html_failed",
            session_id=session_id,
            error=str(exc),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f"HTML export failed: {exc}")


@docs_router.get("/{session_id}/docs")
async def get_session_docs(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Return connector docs.

    Source priority (R2 is source of truth; no MongoDB docs_json):
    1. R2 ``CONNECTOR_DOCS/{provider}/{service_slug}/docs.json``
    2. Local ``.shielva/docs/connector_docs.json`` (dev fallback when R2 not configured)
    """
    logger.info("docs_routes.get", session_id=session_id, tenant_id=x_tenant_id)

    try:
        oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {
            "docs_generated_at": 1,
            "docs_updated_at": 1,
            "doc_prompts": 1,
            "provider": 1,
            "service_slug": 1,
        },
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    docs_json = None
    provider = session.get("provider", "")
    service_slug = session.get("service_slug", "")

    # 1. R2 — primary store
    if provider and service_slug:
        docs_json = await r2_service.get_connector_docs(x_tenant_id, provider, service_slug)
        if docs_json and not docs_json.get("sections"):
            docs_json = None

    # 2. generated_connectors/ local fallback — file is git-tracked via sync request
    if not docs_json:
        docs_json = _read_local_docs(x_tenant_id, service_slug, provider)

    if not docs_json:
        raise HTTPException(
            status_code=404,
            detail="No documentation found for this session. Generate docs first.",
        )

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
    logger.info(
        "docs_routes.code_analysis_generate",
        session_id=session_id,
        tenant_id=x_tenant_id,
    )
    try:
        from integration.services.code_analysis_service import generate_code_analysis

        return await generate_code_analysis(session_id=session_id, tenant_id=x_tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(
            "docs_routes.code_analysis_failed",
            session_id=session_id,
            error=str(exc),
            exc_info=True,
        )
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
        logger.warning(
            "docs_routes.code_analysis_delete_failed",
            session_id=session_id,
            error=str(exc),
        )
        # Non-fatal — return ok=False rather than 500
        return {"deleted": False, "error": str(exc)}
