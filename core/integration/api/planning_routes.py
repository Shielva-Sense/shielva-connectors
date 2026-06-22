"""Integration Builder — Planning API routes."""

from typing import Any, Dict, List, Optional

import structlog
from bson import ObjectId
from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from integration.schemas.models import ReplanRequest
from integration.services.planning_service import (
    approve_plan,
    generate_plan,
    generate_plan_stream,
    get_planning_prompt_text,
    import_cached_plan,
    parse_and_import_plan,
    replan,
    replan_stream,
    refresh_r2_plan,
)

logger = structlog.get_logger(__name__)

planning_router = APIRouter(prefix="/sessions", tags=["planning"])


def _get_tenant(x_tenant_id: Optional[str] = Header(None)) -> Optional[str]:
    """Return tenant_id from header — optional (pre-login sessions use app_id only)."""
    return x_tenant_id or None


@planning_router.get("/{session_id}/plan-prompt")
async def get_plan_prompt(session_id: str, x_tenant_id: Optional[str] = Header(None)):
    """Return the assembled planning prompt text without calling Claude.

    Used by the Electron desktop app to run Claude CLI locally for plan generation.
    """
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, f"Invalid session ID: {session_id}")
    try:
        return await get_planning_prompt_text(session_id, tenant_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        logger.error("planning.prompt_fetch_failed", session_id=session_id, error=str(exc), exc_info=True)
        raise HTTPException(500, f"Failed to assemble planning prompt: {str(exc)}")


class ParsePlanBody(BaseModel):
    raw_output: str  # Raw Claude CLI terminal output


@planning_router.post("/{session_id}/plan/parse")
async def parse_plan_from_raw_output(
    session_id: str,
    body: ParsePlanBody,
    x_tenant_id: Optional[str] = Header(None),
):
    """Parse raw Claude CLI output, extract the JSON plan, and import it.

    Called by the Electron desktop app after local Claude CLI finishes.
    Handles all JSON extraction + validation + MongoDB/R2 persistence server-side.
    """
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, f"Invalid session ID: {session_id}")
    try:
        result = await parse_and_import_plan(session_id, tenant_id, body.raw_output)
        logger.info("planning.parse_complete", session_id=session_id,
                    step_count=len(result.get("steps", [])))
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.error("planning.parse_failed", session_id=session_id, error=str(exc), exc_info=True)
        raise HTTPException(500, f"Plan parse failed: {str(exc)}")


@planning_router.post("/{session_id}/plan")
async def trigger_plan_generation(session_id: str, x_tenant_id: Optional[str] = Header(None)):
    """Generate an AI integration plan for a session."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, f"Invalid session ID: {session_id}")
    logger.info("planning.generate_start", session_id=session_id, tenant_id=tenant_id)
    try:
        plan_data = await generate_plan(session_id, tenant_id)
        logger.info(
            "planning.generate_complete",
            session_id=session_id,
            step_count=len(plan_data.get("steps", [])),
        )
        return plan_data
    except ValueError as exc:
        logger.warning("planning.generate_validation_error", session_id=session_id, error=str(exc))
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.error("planning.generate_failed", session_id=session_id, error=str(exc), exc_info=True)
        raise HTTPException(500, f"Plan generation failed: {str(exc)}")


@planning_router.get("/{session_id}/plan/stream")
async def stream_plan_generation(
    session_id: str,
    prompt: Optional[str] = None,
    app_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    x_tenant_id: Optional[str] = Header(None),
    x_app_id: Optional[str] = Header(None, alias="X-App-ID"),
):
    """Generate a plan with SSE streaming logs for real-time UI feedback.

    Accepts app_id + tenant_id as query params (required for EventSource connections
    which cannot send custom headers). Falls back to X-App-ID / X-Tenant-ID headers.

    Optional query param `prompt`: if provided, the session's user_prompt is updated to this
    value, the R2 plan cache is cleared, and a fresh plan is generated from scratch.
    Used by the frontend "Regenerate Plan" flow to bypass the old plan context.
    """
    resolved_app_id    = app_id or x_app_id
    resolved_tenant_id = tenant_id or x_tenant_id
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, f"Invalid session ID: {session_id}")
    new_prompt = prompt.strip() if prompt and prompt.strip() else None
    logger.info("planning.stream_start", session_id=session_id,
                app_id=resolved_app_id, tenant_id=resolved_tenant_id,
                force_regen=new_prompt is not None)
    return StreamingResponse(
        generate_plan_stream(session_id, resolved_tenant_id, new_prompt=new_prompt, app_id=resolved_app_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class PlanStreamBody(BaseModel):
    prompt: Optional[str] = None


@planning_router.post("/{session_id}/plan/stream")
async def stream_plan_generation_post(
    session_id: str,
    body: PlanStreamBody = Body(default=PlanStreamBody()),
    x_tenant_id: Optional[str] = Header(None),
    x_app_id: Optional[str] = Header(None, alias="X-App-ID"),
):
    """POST variant of plan/stream — accepts the prompt in the request body.

    Preferred over the GET variant for regeneration because large prompts can
    exceed URL length limits when passed as query parameters.
    """
    resolved_app_id    = x_app_id
    resolved_tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, f"Invalid session ID: {session_id}")
    new_prompt = body.prompt.strip() if body.prompt and body.prompt.strip() else None
    logger.info("planning.stream_post_start", session_id=session_id,
                app_id=resolved_app_id, tenant_id=resolved_tenant_id,
                force_regen=new_prompt is not None)
    return StreamingResponse(
        generate_plan_stream(session_id, resolved_tenant_id, new_prompt=new_prompt, app_id=resolved_app_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@planning_router.get("/{session_id}/replan/stream")
async def stream_replan(
    session_id: str,
    step_index: int = 0,
    comment: str = "",
    app_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    x_tenant_id: Optional[str] = Header(None),
    x_app_id: Optional[str] = Header(None, alias="X-App-ID"),
):
    """Regenerate the plan with user feedback, streaming SSE log events for real-time UI.

    Accepts app_id + tenant_id as query params (required for EventSource connections
    which cannot send custom headers). Falls back to X-App-ID / X-Tenant-ID headers.
    """
    resolved_app_id    = app_id or x_app_id
    resolved_tenant_id = tenant_id or x_tenant_id
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, f"Invalid session ID: {session_id}")
    logger.info("planning.replan_stream_start", session_id=session_id,
                app_id=resolved_app_id, step_index=step_index)
    return StreamingResponse(
        replan_stream(session_id, resolved_tenant_id, step_index, comment, app_id=resolved_app_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@planning_router.post("/{session_id}/replan")
async def trigger_replan(session_id: str, body: ReplanRequest, x_tenant_id: Optional[str] = Header(None)):
    """Regenerate the plan with user feedback on a specific step."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, f"Invalid session ID: {session_id}")
    logger.info(
        "planning.replan_start",
        session_id=session_id,
        step_index=body.step_index,
        comment_length=len(body.comment),
    )
    try:
        plan_data = await replan(session_id, tenant_id, body.step_index, body.comment)
        logger.info("planning.replan_complete", session_id=session_id, step_count=len(plan_data.get("steps", [])))
        return plan_data
    except ValueError as exc:
        logger.warning("planning.replan_validation_error", session_id=session_id, error=str(exc))
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.error("planning.replan_failed", session_id=session_id, error=str(exc), exc_info=True)
        raise HTTPException(500, f"Replan failed: {str(exc)}")


class CachedPlanImport(BaseModel):
    steps: List[Dict[str, Any]]
    version: int = 1
    package_structure: Optional[Dict[str, Any]] = None
    recommended_features: Optional[List[Dict[str, Any]]] = None


@planning_router.post("/{session_id}/plan/import")
async def import_plan_from_cache(
    session_id: str,
    body: CachedPlanImport,
    x_tenant_id: Optional[str] = Header(None),
):
    """Import a pre-cached plan into a session without running the LLM."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, f"Invalid session ID: {session_id}")
    try:
        result = await import_cached_plan(session_id, tenant_id, body.model_dump())
        logger.info("planning.plan_imported", session_id=session_id)
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.error("planning.import_failed", session_id=session_id, error=str(exc), exc_info=True)
        raise HTTPException(500, f"Plan import failed: {str(exc)}")


@planning_router.post("/{session_id}/refresh-plan")
async def trigger_refresh_plan(session_id: str, x_tenant_id: Optional[str] = Header(None)):
    """Regenerate the plan using the current CODE_EXECUTION_GUIDELINES and overwrite R2 cache.

    Called automatically on re-execute (after cleanup) so the cached plan.md is always
    in compliance with the latest coding standards.
    """
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, f"Invalid session ID: {session_id}")
    logger.info("planning.refresh_plan_start", session_id=session_id, tenant_id=tenant_id)
    try:
        plan_data = await refresh_r2_plan(session_id, tenant_id)
        logger.info("planning.refresh_plan_complete", session_id=session_id,
                    step_count=len(plan_data.get("steps", [])))
        return plan_data
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.error("planning.refresh_plan_failed", session_id=session_id, error=str(exc), exc_info=True)
        raise HTTPException(500, f"Plan refresh failed: {str(exc)}")


@planning_router.post("/{session_id}/approve")
async def trigger_approve(session_id: str, x_tenant_id: Optional[str] = Header(None)):
    """Approve all steps in the plan and transition to APPROVED status."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, f"Invalid session ID: {session_id}")
    logger.info("planning.approve_start", session_id=session_id, tenant_id=tenant_id)
    try:
        result = await approve_plan(session_id, tenant_id)
        logger.info("planning.approved", session_id=session_id)
        return result
    except ValueError as exc:
        logger.warning("planning.approve_validation_error", session_id=session_id, error=str(exc))
        raise HTTPException(400, str(exc))
