"""Integration Builder — CODE_EXECUTION_GUIDELINES API routes."""

import structlog
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import Optional

from integration.services import guidelines_service
from integration.services.llm_client import call_llm

logger = structlog.get_logger(__name__)

guidelines_router = APIRouter(prefix="/guidelines", tags=["guidelines"])

# ── Request/Response models ───────────────────────────────────────────

class UpdateGuidelinesRequest(BaseModel):
    prompt: str  # User instruction for how to update the guidelines
    current_content: Optional[str] = None  # Pass current content to avoid extra fetch


class GuidelinesResponse(BaseModel):
    version: str
    content: str
    updated_at: str


# ── Routes ────────────────────────────────────────────────────────────

@guidelines_router.get("/connector-development", response_model=GuidelinesResponse)
async def get_connector_development_guidelines():
    """Get the active connector_development.md guidelines."""
    result = await guidelines_service.get_active_guidelines()
    return GuidelinesResponse(**result)


@guidelines_router.post("/connector-development/update")
async def update_connector_development_guidelines(
    body: UpdateGuidelinesRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Update guidelines using an AI prompt.

    Claude reads the current guidelines and the user's instruction,
    then produces an improved version which is saved as a new version.
    """
    current = body.current_content
    if not current:
        current_record = await guidelines_service.get_active_guidelines()
        current = current_record["content"]

    system_prompt = """You are updating the Shielva Connector Development Standard (connector_development.md).

This document defines the standard package structure, coding rules, and practices for all Shielva connectors.

## Current Document
```markdown
{current_content}
```

## User Instruction
{user_prompt}

## Rules
1. Return ONLY the complete updated markdown document
2. Do NOT include markdown code fences around the entire document
3. Preserve all sections unless the user explicitly asks to change them
4. Keep the same heading structure (# ## ###)
5. If the user asks to add/change something, incorporate it while keeping all other sections intact
6. Never remove the Package Structure or Core Rules sections
7. Return the complete document, ready to save as connector_development.md""".format(
        current_content=current,
        user_prompt=body.prompt,
    )

    try:
        updated_content = await call_llm(
            system=system_prompt,
            user="Output the complete updated connector_development.md document.",
        )
        updated_content = updated_content.strip()
        # Strip code fences if LLM wrapped the output
        if updated_content.startswith("```"):
            lines = updated_content.split("\n")
            updated_content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        saved = await guidelines_service.save_guidelines(
            content=updated_content,
            change_description=body.prompt[:200],
        )
        logger.info("guidelines.updated", version=saved["version"], tenant_id=x_tenant_id)
        return saved

    except Exception as exc:
        logger.error("guidelines.update_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to update guidelines: {exc}")


@guidelines_router.get("/connector-development/versions")
async def get_guidelines_versions():
    """Get version history of connector_development.md."""
    versions = await guidelines_service.get_version_history()
    return {"versions": versions}
