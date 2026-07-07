"""Integration Builder — Code generation API routes."""

import json
from collections.abc import AsyncIterator
from datetime import datetime

import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from integration.db.database import sessions_collection
from integration.services import execution_manager
from integration.services.codegen_service import (
    attempt_fix_step,
    execute_plan_sync,
    execute_single_step,
)

logger = structlog.get_logger(__name__)

codegen_router = APIRouter(prefix="/sessions", tags=["codegen"])


@codegen_router.post("/{session_id}/execute")
async def start_execution(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Execute the approved plan (non-streaming). Returns final result."""
    logger.info(
        "codegen.execute_start",
        session_id=session_id,
        tenant_id=x_tenant_id,
        streaming=False,
    )
    try:
        result = await execute_plan_sync(session_id, x_tenant_id)
        logger.info(
            "codegen.execute_complete",
            session_id=session_id,
            status=result.get("status"),
            file_count=result.get("file_count"),
        )
        return result
    except ValueError as exc:
        logger.warning("codegen.execute_validation_error", session_id=session_id, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(
            "codegen.execute_failed",
            session_id=session_id,
            error=str(exc),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))


@codegen_router.get("/{session_id}/execute/stream")
async def stream_execution(
    session_id: str,
    from_event: int = 0,
    skip_llm: bool = Query(False),
    app_id: str | None = None,
    tenant_id: str | None = None,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
    x_app_id: str | None = Header(None, alias="X-App-ID"),
):
    """Execute the approved plan with SSE streaming progress.

    Execution runs as a background task (survives page refresh).
    SSE stream reads from an event buffer — reconnectable via ?from_event=N.

    Accepts app_id + tenant_id as query params (required for EventSource connections
    which cannot send custom headers). Falls back to X-App-ID / X-Tenant-ID headers.

    If execution is already running (e.g., after page refresh), this endpoint
    re-attaches and streams events from where the client left off.
    """
    resolved_app_id = app_id or x_app_id
    resolved_tenant_id = tenant_id or x_tenant_id
    logger.info(
        "codegen.execute_stream_start",
        session_id=session_id,
        app_id=resolved_app_id,
        tenant_id=resolved_tenant_id,
        from_event=from_event,
        already_running=execution_manager.is_running(session_id),
    )

    # Start execution if not already running
    if not execution_manager.is_running(session_id):
        started = await execution_manager.start_execution(
            session_id, resolved_tenant_id, skip_llm=skip_llm, app_id=resolved_app_id
        )
        if not started:
            # Already running from a previous request — just re-attach
            logger.info("codegen.reattaching", session_id=session_id)

    return StreamingResponse(
        execution_manager.stream_events(session_id, from_index=from_event),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@codegen_router.get("/{session_id}/execute/status")
async def execution_status(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Check if execution is running and how many events are buffered."""
    return {
        "running": execution_manager.is_running(session_id),
        "event_count": execution_manager.get_event_count(session_id),
    }


@codegen_router.get("/{session_id}/auto-run/stream")
async def stream_auto_run(
    session_id: str,
    from_event: int = 0,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Auto-run: execute remaining steps, fix failures, skip completed. SSE streaming.

    Runs as background task — survives page refresh.
    """
    if not x_tenant_id or not x_tenant_id.strip():
        raise HTTPException(status_code=400, detail="X-Tenant-ID header is required")
    logger.info("codegen.auto_run_start", session_id=session_id, tenant_id=x_tenant_id)

    if not execution_manager.is_running(session_id):
        await execution_manager.start_auto_run(session_id, x_tenant_id)

    return StreamingResponse(
        execution_manager.stream_events(session_id, from_index=from_event),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@codegen_router.post("/{session_id}/execute/step/{step_index}")
async def retry_single_step(
    session_id: str,
    step_index: int,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Re-execute a single step (e.g., retry a failed step)."""
    logger.info(
        "codegen.retry_step",
        session_id=session_id,
        step_index=step_index,
        tenant_id=x_tenant_id,
    )
    try:
        return await execute_single_step(session_id, x_tenant_id, step_index)
    except ValueError as exc:
        logger.warning("codegen.retry_step_validation_error", session_id=session_id, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(
            "codegen.retry_step_failed",
            session_id=session_id,
            error=str(exc),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))


class AttemptFixRequest(BaseModel):
    error_details: str | None = ""


@codegen_router.post("/{session_id}/fix/step/{step_index}")
async def fix_single_step(
    session_id: str,
    step_index: int,
    body: AttemptFixRequest = AttemptFixRequest(),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Use AI to fix a failed step, then re-run it.

    Returns an SSE stream so the client stays connected during the (potentially
    long) LLM call and gets live log events rather than a silent wait that
    eventually causes a gateway timeout (502 Bad Gateway).

    Events: fix_log {level, message}, fix_complete {result}, fix_error {message}
    """

    def _sse(event: str, data: dict) -> str:
        return f"data: {json.dumps({'type': event, **data}, default=str)}\n\n"

    async def _stream() -> AsyncIterator[str]:
        import asyncio

        logger.info("codegen.fix_step", session_id=session_id, step_index=step_index)
        yield _sse(
            "fix_log",
            {
                "level": "info",
                "message": f"🔧 Starting AI fix for step {step_index + 1}...",
            },
        )

        # Queue-based real-time log streaming:
        # attempt_fix_step calls external_log_cb which puts msgs into this queue.
        # We drain the queue here while the fix task runs so logs appear immediately
        # instead of being buffered until the entire LLM call completes.
        log_queue: asyncio.Queue = asyncio.Queue()

        async def _stream_log(level: str, msg: str):
            await log_queue.put((level, msg))

        try:
            fix_task = asyncio.ensure_future(
                attempt_fix_step(
                    session_id,
                    x_tenant_id,
                    step_index,
                    body.error_details or "",
                    external_log_cb=_stream_log,
                )
            )

            # Drain queue while the fix task is running.
            # Yield ": keepalive" SSE comments every ~300ms when no log arrives —
            # this flushes the HTTP connection through proxies/nginx so the client
            # sees events immediately instead of all at once at the end.
            while not fix_task.done():
                try:
                    level, msg = await asyncio.wait_for(log_queue.get(), timeout=0.3)
                    yield _sse("fix_log", {"level": level, "message": msg})
                except TimeoutError:
                    yield ": keepalive\n\n"  # SSE comment — ignored by client, prevents proxy buffering

            # Drain any remaining logs after task completes
            while not log_queue.empty():
                level, msg = log_queue.get_nowait()
                yield _sse("fix_log", {"level": level, "message": msg})

            result = fix_task.result()  # raises if task raised
            yield _sse("fix_complete", result)

        except ValueError as exc:
            logger.warning(
                "codegen.fix_step_validation_error",
                session_id=session_id,
                error=str(exc),
            )
            yield _sse("fix_error", {"message": str(exc)})
        except Exception as exc:
            import traceback as _tb

            tb_str = _tb.format_exc()
            logger.error(
                "codegen.fix_step_failed",
                session_id=session_id,
                error=str(exc),
                traceback=tb_str,
            )
            yield _sse("fix_error", {"message": str(exc), "traceback": tb_str[-1500:]})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Inject a syntax_check step into an existing plan ─────────────────


@codegen_router.post("/{session_id}/steps/inject-syntax-check")
async def inject_syntax_check_step(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Insert a syntax_check step into the plan immediately after write_connector.

    If the step already exists in the plan it is not duplicated.
    Returns the updated list of plan steps and the index of the injected step.
    """
    try:
        oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    session = await sessions_collection().find_one({"_id": oid, "tenant_id": x_tenant_id})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    steps: list = (session.get("plan") or {}).get("steps", [])

    # Idempotency — don't insert twice
    if any(s.get("type") == "syntax_check" for s in steps):
        return {
            "steps": steps,
            "inserted_index": next(i for i, s in enumerate(steps) if s.get("type") == "syntax_check"),
        }

    # Find insertion point: right after write_connector (or after scaffold_code as fallback)
    insert_after = -1
    for i, s in enumerate(steps):
        if s.get("type") in ("write_connector", "scaffold_code"):
            insert_after = i

    insert_at = insert_after + 1  # defaults to index 0 if neither found

    syntax_step = {
        "index": insert_at,
        "type": "syntax_check",
        "title": "Syntax Check and Apply Fixes",
        "description": "Verify all generated Python files have valid syntax; auto-fix any errors with Gemini before writing tests.",
        "estimated_duration_s": 60,
        "config": {},
        "status": "pending",
    }

    steps.insert(insert_at, syntax_step)

    # Re-index all steps so .index matches list position
    for i, s in enumerate(steps):
        s["index"] = i

    await sessions_collection().update_one(
        {"_id": oid},
        {"$set": {"plan.steps": steps, "updated_at": datetime.utcnow()}},
    )

    logger.info("codegen.inject_syntax_check", session_id=session_id, inserted_at=insert_at)
    return {"steps": steps, "inserted_index": insert_at}
