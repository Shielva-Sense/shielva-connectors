"""Integration Builder — Logs API routes.

Provides endpoints to view service logs and per-session execution logs.
"""

import json
from collections import deque
from pathlib import Path
from typing import Optional

import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException, Query

from integration.db.database import sessions_collection

logger = structlog.get_logger(__name__)

LOG_FILE = Path("logs") / "integration-builder.log"

logs_router = APIRouter(prefix="/logs", tags=["logs"])


@logs_router.get("")
async def get_service_logs(
    lines: int = Query(100, ge=1, le=2000, description="Number of recent log lines to return"),
    level: Optional[str] = Query(None, description="Filter by log level (info, warning, error)"),
    search: Optional[str] = Query(None, description="Search term to filter log entries"),
):
    """Return recent service log entries from the log file.

    Reads the last N lines from the integration-builder log file,
    optionally filtered by level or search term.
    """
    if not LOG_FILE.exists():
        return {"lines": [], "total": 0, "log_file": str(LOG_FILE), "message": "Log file not yet created"}

    # Read last N lines efficiently using a deque
    all_lines = deque(maxlen=lines * 3)  # read extra to allow for filtering
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            all_lines.append(line.rstrip())

    # Parse and filter
    results = []
    for raw_line in all_lines:
        if not raw_line:
            continue
        # Try to parse as JSON (structlog output)
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            entry = {"raw": raw_line}

        # Filter by level
        if level:
            entry_level = entry.get("level", "").lower()
            if entry_level != level.lower():
                continue

        # Filter by search term
        if search:
            line_str = raw_line.lower()
            if search.lower() not in line_str:
                continue

        results.append(entry)

    # Return last N after filtering
    results = results[-lines:]

    logger.info("logs.viewed", line_count=len(results), level_filter=level, search_filter=search)
    return {
        "lines": results,
        "total": len(results),
        "log_file": str(LOG_FILE),
    }


@logs_router.get("/sessions/{session_id}")
async def get_session_execution_logs(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    step_index: Optional[int] = Query(None, description="Filter logs for a specific step"),
):
    """Return persisted execution logs for a session from MongoDB.

    Step execution logs are saved alongside execution_results in the session document.
    """
    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID")

    oid = ObjectId(session_id)
    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"execution_results": 1, "provider": 1, "service": 1, "service_slug": 1, "status": 1},
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    execution_results = session.get("execution_results", [])
    if not execution_results:
        return {
            "session_id": session_id,
            "status": session.get("status"),
            "message": "No execution logs found — execute the plan first",
            "logs": [],
        }

    # Lazy R2 hydration. Step rows written after Phase 3 carry only metadata
    # (step_index/status/duration/etc.) — the heavy `output` + `logs` arrays
    # live at CONNECTORS/{provider}/{slug}/{session_id}/step_outputs/{i}.json.
    # We fetch from R2 only for rows the caller actually wants (one when
    # `step_index` is set, all matching otherwise). Pre-Phase-3 rows that
    # still embed `logs` directly are read in place.
    from integration.services import r2_service as _r2
    provider = session.get("provider", "")
    service_slug = session.get("service_slug") or session.get("service") or ""

    all_logs = []
    for result in execution_results:
        idx = result.get("step_index")
        if step_index is not None and idx != step_index:
            continue
        step_logs = result.get("logs") or []
        if not step_logs and result.get("r2_offloaded"):
            try:
                r2_payload = await _r2.get_step_output(
                    provider=provider,
                    service_slug=service_slug,
                    session_id=session_id,
                    step_index=int(idx),
                )
                if r2_payload:
                    step_logs = r2_payload.get("logs") or []
            except Exception as exc:
                logger.warning("logs.r2_hydrate_failed", session_id=session_id, step_index=idx, error=str(exc))
        for log_entry in step_logs:
            all_logs.append({
                "step_index": idx,
                "step_status": result.get("status"),
                "duration_ms": result.get("duration_ms"),
                **(log_entry if isinstance(log_entry, dict) else {"message": str(log_entry)}),
            })

    logger.info(
        "logs.session_viewed",
        session_id=session_id,
        log_count=len(all_logs),
        step_filter=step_index,
    )

    return {
        "session_id": session_id,
        "status": session.get("status"),
        "provider": session.get("provider"),
        "service": session.get("service"),
        "step_count": len(execution_results),
        "logs": all_logs,
    }
