"""Integration Builder — Knowledge RAG upload API routes.

Endpoints for uploading code guidelines, SDK documentation, and API specs
that are ingested into MCP's RAG pipeline for context-aware code generation.
"""

import structlog
from fastapi import APIRouter, Header, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional

from integration.services import knowledge_service
from integration.services import docs_guidelines_service

logger = structlog.get_logger(__name__)

knowledge_router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# ── Request/Response models ──────────────────────────────────────────

class UploadKnowledgeRequest(BaseModel):
    content: str
    title: str
    scope: str = "global"  # "global" | "connector"
    provider: Optional[str] = None
    service: Optional[str] = None
    session_id: Optional[str] = None


class UploadPathRequest(BaseModel):
    file_path: str
    title: Optional[str] = None
    scope: str = "global"
    provider: Optional[str] = None
    service: Optional[str] = None
    session_id: Optional[str] = None


class KnowledgeDocResponse(BaseModel):
    doc_id: str
    title: str
    scope: str
    created_at: str


class UpdateDocGuidelinesRequest(BaseModel):
    prompt: str
    current_content: Optional[str] = None


# ── Upload routes ────────────────────────────────────────────────────

@knowledge_router.post("/upload", response_model=KnowledgeDocResponse)
async def upload_knowledge(
    body: UploadKnowledgeRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Upload a markdown knowledge document for RAG ingestion.

    Scope 'global' — shared across all connectors for this tenant.
    Scope 'connector' — specific to one provider/service (requires provider + service).
    """
    if not body.content or not body.content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    if len(body.content) > 10 * 1024 * 1024:  # 10MB max
        raise HTTPException(status_code=413, detail="Content too large (max 10MB)")
    if body.scope not in ("global", "connector"):
        raise HTTPException(status_code=400, detail="scope must be 'global' or 'connector'")
    if body.scope == "connector" and (not body.provider or not body.service):
        raise HTTPException(
            status_code=400,
            detail="provider and service are required for scope='connector'",
        )

    try:
        if body.scope == "global":
            result = await knowledge_service.ingest_global_guidelines(
                content=body.content,
                title=body.title,
                tenant_id=x_tenant_id,
            )
        else:
            result = await knowledge_service.ingest_connector_docs(
                content=body.content,
                title=body.title,
                tenant_id=x_tenant_id,
                provider=body.provider or "",
                service=body.service or "",
                session_id=body.session_id,
            )

        return KnowledgeDocResponse(**result)
    except Exception as exc:
        logger.error("knowledge.upload_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")


@knowledge_router.post("/upload-file", response_model=KnowledgeDocResponse)
async def upload_knowledge_file(
    file: UploadFile = File(...),
    scope: str = Form("global"),
    title: Optional[str] = Form(None),
    provider: Optional[str] = Form(None),
    service: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Upload a .md file for RAG ingestion."""
    if not file.filename or not file.filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="Only .md files are supported")

    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid UTF-8 markdown")
    doc_title = title or file.filename.replace(".md", "")

    body = UploadKnowledgeRequest(
        content=text,
        title=doc_title,
        scope=scope,
        provider=provider,
        service=service,
        session_id=session_id,
    )
    return await upload_knowledge(body, x_tenant_id=x_tenant_id)


@knowledge_router.post("/upload-path", response_model=KnowledgeDocResponse)
async def upload_knowledge_path(
    body: UploadPathRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Read a .md file from filesystem path and ingest for RAG."""
    from pathlib import Path

    p = Path(body.file_path).resolve()

    # Path traversal protection: block absolute paths outside the project
    # and reject any path with .. components
    if ".." in str(body.file_path):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {body.file_path}")
    if not p.suffix == ".md":
        raise HTTPException(status_code=400, detail="Only .md files are supported")
    if p.stat().st_size > 10 * 1024 * 1024:  # 10MB max
        raise HTTPException(status_code=413, detail="File too large (max 10MB)")

    text = p.read_text(encoding="utf-8")
    doc_title = body.title or p.stem

    upload_body = UploadKnowledgeRequest(
        content=text,
        title=doc_title,
        scope=body.scope,
        provider=body.provider,
        service=body.service,
        session_id=body.session_id,
    )
    return await upload_knowledge(upload_body, x_tenant_id=x_tenant_id)


# ── List, vector counts, and delete ──────────────────────────────────

@knowledge_router.get("/vector-count")
async def get_vector_count(
    scope: str = "connector",
    provider: Optional[str] = None,
    service: Optional[str] = None,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Get the RAG vector count for a connector or global guidelines KB.

    Visible in Manage > Integration Builder UI so users can see knowledge state.
    """
    if scope == "global":
        return await knowledge_service.get_global_vector_count(x_tenant_id)
    elif provider and service:
        return await knowledge_service.get_connector_vector_count(x_tenant_id, provider, service)
    else:
        raise HTTPException(400, "provider and service are required for scope='connector'")


@knowledge_router.get("/list")
async def list_knowledge(
    scope: str = "all",
    provider: Optional[str] = None,
    service: Optional[str] = None,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """List uploaded knowledge documents for the tenant."""
    docs = await knowledge_service.list_uploaded_docs(
        tenant_id=x_tenant_id,
        scope=scope,
        provider=provider or "",
        service=service or "",
    )
    return {"documents": docs, "count": len(docs)}


@knowledge_router.delete("/{doc_id}")
async def delete_knowledge(
    doc_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Delete a knowledge document."""
    deleted = await knowledge_service.delete_doc(doc_id, x_tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"status": "deleted", "doc_id": doc_id}


# ── Documentation guidelines (R2 fallback) ───────────────────────────

@knowledge_router.get("/doc-guidelines")
async def get_doc_guidelines():
    """Get the active connector documentation guidelines (R2 fallback template)."""
    result = await docs_guidelines_service.get_active_doc_guidelines()
    return result


@knowledge_router.post("/doc-guidelines/update")
async def update_doc_guidelines(
    body: UpdateDocGuidelinesRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Update documentation guidelines using an AI prompt."""
    from integration.services.llm_client import call_llm

    current = body.current_content
    if not current:
        current_record = await docs_guidelines_service.get_active_doc_guidelines()
        current = current_record["content"]

    system_prompt = (
        "You are updating the Shielva Connector Documentation Standard.\n\n"
        "## Current Document\n```markdown\n{current}\n```\n\n"
        "## User Instruction\n{prompt}\n\n"
        "## Rules\n"
        "1. Return ONLY the complete updated markdown document\n"
        "2. Do NOT include markdown code fences around the entire document\n"
        "3. Preserve all sections unless the user explicitly asks to change them\n"
        "4. Keep the same heading structure (# ## ###)\n"
        "5. Return the complete document, ready to save"
    ).format(current=current, prompt=body.prompt)

    try:
        updated_content = await call_llm(
            messages=[{"role": "user", "content": "Output the complete updated connector documentation guidelines."}],
            system=system_prompt,
            expect_code=False,
        )
        updated_content = updated_content.strip()
        if updated_content.startswith("```"):
            lines = updated_content.split("\n")
            updated_content = "\n".join(
                lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            )

        saved = await docs_guidelines_service.save_doc_guidelines(
            content=updated_content,
            change_description=body.prompt[:200],
        )
        logger.info("doc_guidelines.updated", version=saved["version"], tenant_id=x_tenant_id)
        return saved

    except Exception as exc:
        logger.error("doc_guidelines.update_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to update guidelines: {exc}")


@knowledge_router.get("/doc-guidelines/versions")
async def get_doc_guidelines_versions():
    """Get version history of documentation guidelines."""
    versions = await docs_guidelines_service.get_doc_guidelines_version_history()
    return {"versions": versions}
