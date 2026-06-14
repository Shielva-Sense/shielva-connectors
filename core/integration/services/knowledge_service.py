"""Integration Builder — Knowledge RAG service.

Manages uploading and querying knowledge documents (code guidelines, SDK docs,
API specs) via MCP's RAG pipeline. Documents are:
  1. Stored in MongoDB metadata (title, scope, tenant, timestamps)
  2. Ingested into MCP's vector store via the ingestion worker
  3. Queried during code generation to provide contextual knowledge

Two scopes:
  - "global"    — code guidelines / standards shared across all connectors for a tenant
  - "connector" — SDK docs, API specs specific to one connector (provider/service)
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
import structlog

from integration.core.config import settings

logger = structlog.get_logger(__name__)

# KB ID patterns — deterministic so we can query without a lookup
_GLOBAL_KB_ID = "codegen-guidelines-{tenant_id}"
_CONNECTOR_KB_ID = "codegen-docs-{tenant_id}-{provider}-{service}"


def _mcp_headers(tenant_id: str) -> Dict[str, str]:
    """Canonical internal service-to-service headers for MCP calls.

    Per the platform contract (docs/architecture/MCP_LLM_BROKER.md → "Auth
    (internal service-to-service)"), internal callers identify with the REAL
    per-request tenant plus the internal-service identity. MCP's
    ``require_principal`` needs user-id + email (this was the missing
    ``X-User-Email`` causing the 401 principal_header_missing). Mirrors the
    integration's own ``mcp_client.py`` client.
    """
    return {
        # Legacy form (consumed by codegen-style header readers)
        "X-Tenant-ID": tenant_id,
        "X-User-ID": "integration-builder",
        "X-User-Email": "internal@shielva.ai",
        "X-Auth-Method": "service",
        # Canonical X-Shielva-* form (consumed by require_principal on the tools
        # path — it needs user-id + email + auth-method).
        "X-Shielva-Tenant-Id": tenant_id,
        "X-Shielva-User-Id": "integration-builder",
        "X-Shielva-Email": "internal@shielva.ai",
        "X-Shielva-Auth-Method": "service",
        "Content-Type": "application/json",
    }


# ── MongoDB helpers ──────────────────────────────────────────────────

def _knowledge_collection():
    from integration.db.database import get_db
    return get_db()["connector_knowledge_docs"]


# ── KB ID builders ───────────────────────────────────────────────────

def _global_kb_id(tenant_id: str) -> str:
    return _GLOBAL_KB_ID.format(tenant_id=tenant_id)


def _connector_kb_id(tenant_id: str, provider: str, service: str) -> str:
    # Sanitize: lowercase, replace non-alphanumeric with underscore (collision-safe)
    safe_provider = re.sub(r'[^a-z0-9]+', '_', provider.lower().strip()).strip('_')
    safe_service = re.sub(r'[^a-z0-9]+', '_', service.lower().strip()).strip('_')
    return _CONNECTOR_KB_ID.format(
        tenant_id=tenant_id,
        provider=safe_provider,
        service=safe_service,
    )


# ── Ingest via MCP ingestion worker ─────────────────────────────────

async def _ingest_to_mcp(
    content: str,
    title: str,
    kb_id: str,
    tenant_id: str,
    doc_id: Optional[str] = None,
) -> str:
    """Send a markdown document to MCP's ingestion worker for RAG indexing.

    Returns the document ID used.
    """
    doc_id = doc_id or str(uuid.uuid4())
    url = f"{settings.MCP_INGESTION_URL}/ingest/sync"

    payload = {
        "kb_id": kb_id,
        "documents": [
            {
                "id": doc_id,
                "content": content,
                "title": title,          # required top-level field by IngestDocumentRequest
                "doc_type": "text",
                "metadata": {
                    "source": "integration-builder-upload",
                    "type": "markdown",
                },
            }
        ],
    }

    headers = _mcp_headers(tenant_id)

    logger.info(
        "knowledge.ingesting",
        kb_id=kb_id,
        doc_id=doc_id,
        title=title,
        content_length=len(content),
        tenant_id=tenant_id,
    )

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info("knowledge.ingested", kb_id=kb_id, doc_id=doc_id, status=resp.status_code)
    except httpx.HTTPStatusError as exc:
        # Don't crash — log the error and continue. The document metadata
        # is still stored in MongoDB so it can be retried later.
        logger.warning(
            "knowledge.ingest_http_error",
            status=exc.response.status_code,
            body=exc.response.text[:300],
            kb_id=kb_id,
            doc_id=doc_id,
        )
        logger.info("knowledge.stored_without_rag", doc_id=doc_id,
                     reason=f"HTTP {exc.response.status_code}")
    except httpx.RequestError as exc:
        # MCP ingestion worker unreachable — degrade gracefully
        logger.warning(
            "knowledge.ingest_connection_error",
            error=str(exc),
            url=url,
        )
        logger.info("knowledge.stored_without_rag", doc_id=doc_id,
                     reason="connection_error")

    return doc_id


# ── Public API ───────────────────────────────────────────────────────

async def ingest_global_guidelines(
    content: str,
    title: str,
    tenant_id: str,
) -> Dict[str, Any]:
    """Upload global code guidelines/standards shared across all connectors.

    Returns metadata dict.
    """
    kb_id = _global_kb_id(tenant_id)
    doc_id = str(uuid.uuid4())

    # Ingest into MCP RAG (best-effort)
    await _ingest_to_mcp(content, title, kb_id, tenant_id, doc_id)

    # Store metadata in MongoDB
    col = _knowledge_collection()
    now = datetime.now(timezone.utc)
    doc = {
        "doc_id": doc_id,
        "kb_id": kb_id,
        "title": title,
        "scope": "global",
        "tenant_id": tenant_id,
        "provider": None,
        "service": None,
        "content_preview": content[:500],
        "content_length": len(content),
        "created_at": now,
    }
    await col.insert_one(doc)
    logger.info("knowledge.global_saved", doc_id=doc_id, title=title, tenant_id=tenant_id)
    return {"doc_id": doc_id, "title": title, "scope": "global", "created_at": str(now)}


async def ingest_connector_docs(
    content: str,
    title: str,
    tenant_id: str,
    provider: str,
    service: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload SDK docs or API specs specific to one connector.

    Returns metadata dict.
    """
    kb_id = _connector_kb_id(tenant_id, provider, service)
    doc_id = str(uuid.uuid4())

    await _ingest_to_mcp(content, title, kb_id, tenant_id, doc_id)

    col = _knowledge_collection()
    now = datetime.now(timezone.utc)
    doc = {
        "doc_id": doc_id,
        "kb_id": kb_id,
        "title": title,
        "scope": "connector",
        "tenant_id": tenant_id,
        "provider": provider,
        "service": service,
        "session_id": session_id,
        "content_preview": content[:500],
        "content_length": len(content),
        "created_at": now,
    }
    await col.insert_one(doc)
    logger.info(
        "knowledge.connector_saved",
        doc_id=doc_id,
        title=title,
        provider=provider,
        service=service,
    )
    return {"doc_id": doc_id, "title": title, "scope": "connector", "created_at": str(now)}


async def query_knowledge(
    query: str,
    tenant_id: str,
    provider: str = "",
    service: str = "",
    top_k: int = 10,
) -> str:
    """Query MCP RAG for relevant knowledge chunks.

    Searches global guidelines KB (shared code standards + doc standards),
    tenant-specific guidelines KB, and connector-specific KB.
    Returns formatted string to inject into LLM prompts.
    """
    kb_ids = [
        "codegen-guidelines-global",        # shared code + doc guidelines (ingested on startup)
        _global_kb_id(tenant_id),            # tenant-uploaded global guidelines
    ]
    if provider and service:
        kb_ids.append(_connector_kb_id(tenant_id, provider, service))

    # Call MCP's rag_query tool via the codegen endpoint
    url = f"{settings.MCP_URL}/mcp/v1/tools/rag_query/execute"
    payload = {
        "tool_name": "rag_query",
        "parameters": {
            "query": query,
            "kb_ids": kb_ids,
            "top_k": top_k,
        },
    }
    headers = _mcp_headers(tenant_id)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        results = data.get("result", {}).get("results", [])
        if not results:
            return ""

        # Score threshold: drop chunks scoring below 35% of the top result's score.
        # Prevents low-confidence noise from being injected into the LLM prompt.
        top_score = results[0].get("score", 1.0) if results else 1.0
        min_score = top_score * 0.35
        before_count = len(results)
        results = [r for r in results if r.get("score", 0.0) >= min_score]
        if len(results) < before_count:
            logger.debug(
                "knowledge.score_threshold_applied",
                before=before_count,
                after=len(results),
                min_score=round(min_score, 4),
                top_score=round(top_score, 4),
            )

        if not results:
            return ""

        # Format results as context block
        chunks = []
        for i, r in enumerate(results, 1):
            content = r.get("content", "")
            source = r.get("source", "Knowledge Base")
            score = r.get("score", 0.0)
            chunks.append(
                f"<knowledge_chunk_{i} source=\"{source}\" relevance=\"{score:.2f}\">\n"
                f"{content}\n"
                f"</knowledge_chunk_{i}>"
            )

        return "\n\n".join(chunks)

    except Exception as exc:
        logger.warning("knowledge.query_failed", error=str(exc), query=query[:100])
        return ""  # Graceful fallback — don't block code generation


async def list_uploaded_docs(
    tenant_id: str,
    scope: str = "all",
    provider: str = "",
    service: str = "",
) -> List[Dict[str, Any]]:
    """List all uploaded knowledge documents for a tenant.

    scope: "global" | "connector" | "all"
    """
    col = _knowledge_collection()
    query_filter: Dict[str, Any] = {"tenant_id": tenant_id}

    if scope == "global":
        query_filter["scope"] = "global"
    elif scope == "connector":
        query_filter["scope"] = "connector"
        if provider:
            query_filter["provider"] = provider
        if service:
            query_filter["service"] = service

    cursor = col.find(query_filter, {"_id": 0, "content_preview": 0}).sort("created_at", -1).limit(100)
    docs = await cursor.to_list(length=100)

    return [
        {
            "doc_id": d.get("doc_id"),
            "title": d.get("title"),
            "scope": d.get("scope"),
            "provider": d.get("provider"),
            "service": d.get("service"),
            "content_length": d.get("content_length", 0),
            "created_at": str(d.get("created_at", "")),
        }
        for d in docs
    ]


async def delete_doc(doc_id: str, tenant_id: str) -> bool:
    """Delete a knowledge document by ID.

    Removes from MongoDB. MCP RAG deletion is best-effort.
    """
    col = _knowledge_collection()
    result = await col.delete_one({"doc_id": doc_id, "tenant_id": tenant_id})

    if result.deleted_count > 0:
        logger.info("knowledge.deleted", doc_id=doc_id, tenant_id=tenant_id)
        # TODO: Delete from MCP RAG vector store (requires ingestion worker delete API)
        return True

    logger.warning("knowledge.delete_not_found", doc_id=doc_id, tenant_id=tenant_id)
    return False


# ── Per-connector RAG lifecycle ──────────────────────────────────────

async def ingest_step_output(
    content: str,
    filename: str,
    tenant_id: str,
    provider: str,
    service: str,
    step_type: str,
) -> Optional[str]:
    """Ingest a generated file into the per-connector RAG KB.

    Called after each execution step completes. This builds up the connector's
    knowledge base incrementally so the LLM always has full context.
    Skips empty content to avoid polluting the vector index.

    For Python files, AST metadata (class and function names) is extracted
    and stored as metadata to enable precise future retrieval (P3).

    Returns doc_id or None on failure.
    """
    # Guard: reject empty/whitespace-only content to avoid polluting the vector index
    if not content or not content.strip():
        logger.debug("knowledge.ingest_step_output_empty", filename=filename, step_type=step_type)
        return None

    kb_id = _connector_kb_id(tenant_id, provider, service)
    doc_id = f"{step_type}_{filename}".replace("/", "_").replace(".", "_")
    title = f"{step_type}: {filename}"

    # P3: Build enhanced payload for .py files — extract AST structure
    extra_metadata: Dict[str, Any] = {}
    if filename.endswith(".py"):
        import ast as _ast
        try:
            tree = _ast.parse(content)
            classes = [n.name for n in _ast.walk(tree) if isinstance(n, _ast.ClassDef)]
            functions = [
                n.name for n in _ast.walk(tree)
                if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
            ]
            extra_metadata["classes"] = classes
            extra_metadata["functions"] = functions
            logger.debug(
                "knowledge.ast_extracted",
                filename=filename,
                classes=classes,
                functions=functions,
            )
        except SyntaxError:
            pass  # malformed file — skip AST, still ingest content

    # We pass extra_metadata via the title for now (MCP ingestion payload metadata is opaque).
    # If the ingestion worker supports rich metadata, this is where you'd extend _ingest_to_mcp.
    # For now we encode a short hint in the title so vector search benefits from it.
    if extra_metadata.get("classes") or extra_metadata.get("functions"):
        symbols = ", ".join(
            (extra_metadata.get("classes") or []) + (extra_metadata.get("functions") or [])
        )
        title = f"{title} [{symbols}]"

    try:
        await _ingest_to_mcp(content, title, kb_id, tenant_id, doc_id)
        logger.info(
            "knowledge.step_output_ingested",
            step_type=step_type,
            filename=filename,
            kb_id=kb_id,
            ast_classes=extra_metadata.get("classes", []),
            ast_functions=extra_metadata.get("functions", []),
        )
    except Exception as exc:
        logger.warning(
            "knowledge.step_output_ingest_failed",
            step_type=step_type,
            filename=filename,
            error=str(exc),
        )
        return None

    # Upsert MongoDB metadata so doc_count reflects the real indexed file count.
    # Use doc_id as the unique key — re-ingesting the same file updates the record.
    try:
        col = _knowledge_collection()
        now = datetime.now(timezone.utc)
        await col.update_one(
            {"doc_id": doc_id, "tenant_id": tenant_id},
            {"$set": {
                "doc_id": doc_id,
                "kb_id": kb_id,
                "title": title,
                "scope": "connector",
                "tenant_id": tenant_id,
                "provider": provider,
                "service": service,
                "step_type": step_type,
                "filename": filename,
                "content_preview": content[:300],
                "content_length": len(content),
                "updated_at": now,
                **extra_metadata,
            }, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception as exc:
        logger.warning("knowledge.step_output_mongo_upsert_failed", doc_id=doc_id, error=str(exc))

    return doc_id


async def cleanup_connector_knowledge(
    tenant_id: str,
    provider: str,
    service: str,
) -> int:
    """Delete ALL per-connector RAG knowledge when connector is deleted/regenerated.

    Removes:
    1. All MongoDB metadata records for this connector
    2. The MCP RAG knowledge base (via ingestion worker DELETE /kb/{kb_id})

    Returns count of deleted MongoDB records.
    """
    kb_id = _connector_kb_id(tenant_id, provider, service)

    # 1. Delete from MongoDB (wrapped in try/except so MCP cleanup still runs on failure)
    deleted_count = 0
    col = _knowledge_collection()
    try:
        result = await col.delete_many({
            "tenant_id": tenant_id,
            "kb_id": kb_id,
        })
        deleted_count = result.deleted_count
    except Exception as mongo_err:
        logger.warning("knowledge.kb_delete_mongo_failed", kb_id=kb_id, error=str(mongo_err))

    # 2. Delete the KB from MCP ingestion worker (best-effort)
    try:
        url = f"{settings.MCP_INGESTION_URL}/kb/{kb_id}"
        headers = _mcp_headers(tenant_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(url, headers=headers)
            if resp.status_code in (200, 204, 404):
                logger.info("knowledge.kb_deleted_from_rag", kb_id=kb_id)
            else:
                logger.warning(
                    "knowledge.kb_delete_rag_error",
                    kb_id=kb_id,
                    status=resp.status_code,
                )
    except Exception as exc:
        logger.warning("knowledge.kb_delete_rag_failed", kb_id=kb_id, error=str(exc))

    logger.info(
        "knowledge.connector_cleaned",
        tenant_id=tenant_id,
        provider=provider,
        service=service,
        deleted_mongo=deleted_count,
    )
    return deleted_count


async def get_connector_vector_count(
    tenant_id: str,
    provider: str,
    service: str,
) -> Dict[str, Any]:
    """Get the count of RAG vectors for a specific connector.

    Returns: {"kb_id": "...", "vector_count": N, "doc_count": N}
    """
    kb_id = _connector_kb_id(tenant_id, provider, service)

    # Count MongoDB metadata records
    col = _knowledge_collection()
    doc_count = await col.count_documents({"tenant_id": tenant_id, "kb_id": kb_id})

    # Try to get vector count from MCP ingestion worker
    vector_count = 0
    try:
        url = f"{settings.MCP_INGESTION_URL}/kb/{kb_id}/info"
        headers = _mcp_headers(tenant_id)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                vector_count = data.get("vector_count", data.get("document_count", 0))
    except Exception as exc:
        logger.debug("knowledge.vector_count_failed", kb_id=kb_id, error=str(exc))

    return {"kb_id": kb_id, "vector_count": vector_count, "doc_count": doc_count}


async def get_global_vector_count(tenant_id: str) -> Dict[str, Any]:
    """Get the count of RAG vectors for global guidelines KB."""
    kb_id = _global_kb_id(tenant_id)
    col = _knowledge_collection()
    doc_count = await col.count_documents({"tenant_id": tenant_id, "kb_id": kb_id})

    vector_count = 0
    try:
        url = f"{settings.MCP_INGESTION_URL}/kb/{kb_id}/info"
        headers = _mcp_headers(tenant_id)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                vector_count = data.get("vector_count", data.get("document_count", 0))
    except Exception:
        pass

    return {"kb_id": kb_id, "vector_count": vector_count, "doc_count": doc_count}
