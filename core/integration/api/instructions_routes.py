"""Integration Builder — Setup Instructions API routes.

Endpoints for reading, regenerating, and chatting with connector setup instructions.
"""

import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from integration.core.config import settings
from integration.db.database import sessions_collection
from integration.services import knowledge_service, r2_service
from integration.services.llm_client import call_llm_fix

logger = structlog.get_logger(__name__)

instructions_router = APIRouter(prefix="/sessions", tags=["instructions"])


def _get_tenant(x_tenant_id: str | None) -> str:
    if not x_tenant_id:
        raise HTTPException(400, "X-Tenant-ID header is required")
    return x_tenant_id


def _output_dir(tenant_id: str, service_slug: str):
    import re as _re
    from pathlib import Path

    base = Path(settings.GENERATED_CODE_DIR).resolve()
    clean = _re.sub(r"_connector$", "", service_slug) if service_slug.endswith("_connector") else service_slug
    return base / tenant_id / f"{clean}_connector"


# ── Request / Response models ─────────────────────────────────────────


class InstructionsChatRequest(BaseModel):
    message: str
    history: list[dict[str, str]] = []  # [{role, content}]


class InstructionsGenerateRequest(BaseModel):
    prompt: str  # user instruction to update/append/regenerate


# ── GET instructions content ──────────────────────────────────────────


@instructions_router.get("/{session_id}/instructions")
async def get_instructions(
    session_id: str,
    x_tenant_id: str | None = Header(None),
):
    """Return the content of instructions/setup.md for this session's connector."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    doc = await sessions_collection().find_one(
        {"_id": ObjectId(session_id), "tenant_id": tenant_id},
        {"service_slug": 1, "provider": 1, "service": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    service_slug = doc.get("service_slug", "")
    provider = doc.get("provider", "")
    if not service_slug:
        raise HTTPException(422, "service_slug not set on session")

    out_dir = _output_dir(tenant_id, service_slug)
    # Check canonical path first, then root-level fallback (Claude sometimes writes here)
    for candidate in (
        out_dir / "instructions" / "setup.md",
        out_dir / "setup_instructions.md",
        out_dir / "instructions" / "setup_instructions.md",
    ):
        if candidate.exists():
            content = candidate.read_text(encoding="utf-8")
            return {"content": content, "exists": True, "path": str(candidate)}

    # Fallback: load from disk-first cache (R2 secondary)
    cached = await r2_service.get_setup_instructions(provider, service_slug)
    if cached:
        return {
            "content": cached,
            "exists": True,
            "path": f"cache:{provider}/{service_slug}/setup_instructions.md",
        }

    return {
        "content": "",
        "exists": False,
        "path": str(out_dir / "instructions" / "setup.md"),
    }


# ── POST chat (RAG Q&A) ───────────────────────────────────────────────


@instructions_router.post("/{session_id}/instructions/chat")
async def chat_instructions(
    session_id: str,
    body: InstructionsChatRequest,
    x_tenant_id: str | None = Header(None),
):
    """Answer a user question about setup instructions using RAG + LLM."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    doc = await sessions_collection().find_one(
        {"_id": ObjectId(session_id), "tenant_id": tenant_id},
        {"service_slug": 1, "provider": 1, "service": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    provider = doc.get("provider", "")
    service = doc.get("service", doc.get("service_slug", ""))

    # Query RAG for relevant knowledge (includes ingested setup.md)
    rag_context = await knowledge_service.query_knowledge(
        query=body.message,
        tenant_id=tenant_id,
        provider=provider,
        service=service,
        top_k=8,
    )

    # Also load the raw instructions file as primary context
    service_slug = doc.get("service_slug", "")
    instructions_content = ""
    if service_slug:
        ip = _output_dir(tenant_id, service_slug) / "instructions" / "setup.md"
        if ip.exists():
            instructions_content = ip.read_text(encoding="utf-8")

    system = (
        "You are a helpful setup assistant for the Shielva connector platform.\n"
        "You have access to the connector's setup instructions and knowledge base.\n"
        "Answer the user's question clearly and concisely, referencing specific steps from the instructions.\n"
        "If the answer is not in the instructions, say so and provide a best-effort answer.\n\n"
        + (
            f"## Setup Instructions\n```markdown\n{instructions_content[:6000]}\n```\n\n"
            if instructions_content
            else ""
        )
        + (f"## Additional Knowledge\n{rag_context}\n" if rag_context else "")
    )

    messages: list[dict[str, str]] = []
    for h in body.history[-6:]:  # last 6 turns
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
    messages.append({"role": "user", "content": body.message})

    try:
        answer = await call_llm_fix(messages, system=system, max_tokens=2000)
        return {"answer": answer.strip()}
    except Exception as e:
        logger.error("instructions.chat_failed", error=str(e), session_id=session_id)
        raise HTTPException(500, f"Chat failed: {e}")


# ── POST generate/update instructions with prompt ─────────────────────


@instructions_router.post("/{session_id}/instructions/generate")
async def generate_instructions(
    session_id: str,
    body: InstructionsGenerateRequest,
    x_tenant_id: str | None = Header(None),
):
    """Regenerate or update setup instructions based on a user prompt."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    doc = await sessions_collection().find_one(
        {"_id": ObjectId(session_id), "tenant_id": tenant_id},
        {"service_slug": 1, "provider": 1, "service": 1, "connector_name": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    service_slug = doc.get("service_slug", "")
    if not service_slug:
        raise HTTPException(422, "service_slug not set on session")

    out_dir = _output_dir(tenant_id, service_slug)
    instructions_path = out_dir / "instructions" / "setup.md"

    # Load current instructions (may be empty for first generation)
    current_content = ""
    if instructions_path.exists():
        current_content = instructions_path.read_text(encoding="utf-8")

    # Load connector context
    connector_source = ""
    cp = out_dir / "connector.py"
    if cp.exists():
        connector_source = cp.read_text(encoding="utf-8")[:4000]

    metadata_content = ""
    mp = out_dir / "metadata" / "connector.json"
    if mp.exists():
        metadata_content = mp.read_text(encoding="utf-8")[:2000]

    system = (
        "You are a technical documentation expert updating setup instructions for a Shielva connector.\n"
        "The user will give you an instruction to modify or regenerate the setup guide.\n"
        "Return ONLY the complete updated markdown document — no code fences around it.\n"
        "Keep all correct existing sections. Apply the user's changes precisely.\n"
    )
    user_msg = (
        f"## Current Instructions\n```markdown\n{current_content}\n```\n\n"
        f"## Connector Source (excerpt)\n```python\n{connector_source}\n```\n\n"
        f"## Metadata\n```json\n{metadata_content}\n```\n\n"
        f"## User Instruction\n{body.prompt}\n\n"
        "Return the complete updated instructions/setup.md content."
    )

    try:
        updated = await call_llm_fix(
            [{"role": "user", "content": user_msg}],
            system=system,
            max_tokens=8000,
        )
        updated = updated.strip()
        # Strip accidental code fences
        if updated.startswith("```markdown"):
            updated = updated[len("```markdown") :].strip()
        if updated.startswith("```"):
            updated = updated[3:].strip()
        if updated.endswith("```"):
            updated = updated[:-3].strip()

        # Write to disk
        instructions_path.parent.mkdir(parents=True, exist_ok=True)
        instructions_path.write_text(updated, encoding="utf-8")

        # Re-ingest into RAG
        try:
            await knowledge_service.ingest_step_output(
                content=updated,
                filename="instructions/setup.md",
                tenant_id=tenant_id,
                provider=doc.get("provider", ""),
                service=doc.get("service", service_slug),
                step_type="setup_instructions",
            )
        except Exception as _e:
            logger.warning("instructions.reingest_failed", error=str(_e))

        logger.info("instructions.generated", session_id=session_id, chars=len(updated))
        return {"content": updated, "chars": len(updated)}

    except Exception as e:
        logger.error("instructions.generate_failed", error=str(e), session_id=session_id)
        raise HTTPException(500, f"Generation failed: {e}")
