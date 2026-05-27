"""Integration Builder — Background Execution Manager.

Decouples execution from the HTTP/SSE connection lifecycle so that:
  1. Page refresh does NOT stop execution — it runs as a background task
  2. SSE clients can reconnect and resume receiving events from where they left off
  3. All events are persisted in a buffer (Redis or in-memory) for replay on reconnect

Architecture:
  - `start_execution()` launches the execution as an `asyncio.Task` (independent of HTTP)
  - Events from `execute_plan()` generator are drained into a per-session event buffer
  - `stream_events()` reads from the buffer and yields SSE — reconnectable
  - `is_running()` checks if a session has an active execution task
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, Optional

import structlog

logger = structlog.get_logger(__name__)

# In-memory execution state — keyed by session_id
# Each entry: { "task": asyncio.Task, "events": list[str], "done": bool, "started_at": float }
_EXECUTIONS: Dict[str, Dict[str, Any]] = {}

# Max events to buffer per session (prevent unbounded memory growth)
_MAX_EVENTS = 2000


def _sse_event(event_type: str, data: Dict[str, Any]) -> str:
    """Format SSE event string."""
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


async def start_execution(
    session_id: str,
    tenant_id: Optional[str],
    from_step_index: int = 0,
    force_restart: bool = False,
    skip_llm: bool = False,
    app_id: Optional[str] = None,
) -> bool:
    """Start execution as a background task (survives HTTP disconnect).

    Returns True if execution was started, False if already running.
    """
    # Check if already running
    existing = _EXECUTIONS.get(session_id)
    if existing and not existing.get("done") and existing.get("task") and not existing["task"].done():
        logger.info("exec_manager.already_running", session_id=session_id)
        return False

    # Create event buffer
    _EXECUTIONS[session_id] = {
        "task": None,
        "events": [],
        "done": False,
        "started_at": time.time(),
        "tenant_id": tenant_id,
        "app_id": app_id,
    }

    # Import here to avoid circular imports
    from integration.services.codegen_service import execute_plan

    async def _drain_generator():
        """Consume the execute_plan generator and buffer all events."""
        try:
            logger.info("exec_manager.task_started", session_id=session_id)
            async for event_str in execute_plan(session_id, tenant_id, from_step_index, force_restart, skip_llm=skip_llm):
                buf = _EXECUTIONS.get(session_id)
                if buf is None:
                    break  # session was cleaned up
                if len(buf["events"]) < _MAX_EVENTS:
                    buf["events"].append(event_str)
            logger.info("exec_manager.task_completed", session_id=session_id)
        except asyncio.CancelledError:
            logger.warning("exec_manager.task_cancelled", session_id=session_id)
            buf = _EXECUTIONS.get(session_id)
            if buf:
                buf["events"].append(_sse_event("execution_error", {
                    "message": "Execution was cancelled",
                }))
        except Exception as exc:
            logger.error("exec_manager.task_failed", session_id=session_id, error=str(exc))
            buf = _EXECUTIONS.get(session_id)
            if buf:
                buf["events"].append(_sse_event("execution_error", {
                    "message": f"Execution failed: {type(exc).__name__}",
                }))
        finally:
            buf = _EXECUTIONS.get(session_id)
            if buf:
                buf["done"] = True

    # Launch as independent background task
    task = asyncio.create_task(_drain_generator())
    _EXECUTIONS[session_id]["task"] = task
    logger.info("exec_manager.started", session_id=session_id, tenant_id=tenant_id)
    return True


async def start_auto_run(session_id: str, tenant_id: str) -> bool:
    """Start auto-run as a background task (same pattern as execution)."""
    existing = _EXECUTIONS.get(session_id)
    if existing and not existing.get("done") and existing.get("task") and not existing["task"].done():
        return False

    _EXECUTIONS[session_id] = {
        "task": None,
        "events": [],
        "done": False,
        "started_at": time.time(),
        "tenant_id": tenant_id,
    }

    from integration.services.codegen_service import auto_run_session

    async def _drain():
        try:
            async for event_str in auto_run_session(session_id, tenant_id):
                buf = _EXECUTIONS.get(session_id)
                if buf is None:
                    break
                if len(buf["events"]) < _MAX_EVENTS:
                    buf["events"].append(event_str)
        except Exception as exc:
            logger.error("exec_manager.auto_run_failed", session_id=session_id, error=str(exc))
            buf = _EXECUTIONS.get(session_id)
            if buf:
                buf["events"].append(_sse_event("execution_error", {
                    "message": f"Auto-run failed: {type(exc).__name__}",
                }))
        finally:
            buf = _EXECUTIONS.get(session_id)
            if buf:
                buf["done"] = True

    task = asyncio.create_task(_drain())
    _EXECUTIONS[session_id]["task"] = task
    return True


async def stream_events(
    session_id: str,
    from_index: int = 0,
) -> AsyncGenerator[str, None]:
    """Yield SSE events from the buffer, starting at from_index.

    If execution is still running, waits for new events via polling.
    If execution is done, yields remaining events and returns.
    Reconnectable: pass from_index = len(previously_received_events).
    """
    buf = _EXECUTIONS.get(session_id)
    if buf is None:
        yield _sse_event("error", {"message": "No execution found for this session"})
        return

    cursor = from_index

    while True:
        events = buf["events"]
        done = buf["done"]

        # Yield all buffered events from cursor
        while cursor < len(events):
            yield events[cursor]
            cursor += 1

        # If done, send final marker and exit
        if done:
            yield _sse_event("stream_end", {"total_events": cursor, "session_id": session_id})
            return

        # Wait for new events (100ms polling — keeps SSE alive)
        await asyncio.sleep(0.1)

        # Send keepalive comment every ~3s to prevent proxy timeout
        if cursor == len(buf["events"]):
            yield ": keepalive\n\n"


def is_running(session_id: str) -> bool:
    """Check if a session has an active execution task."""
    buf = _EXECUTIONS.get(session_id)
    if not buf:
        return False
    if buf.get("done"):
        return False
    task = buf.get("task")
    return task is not None and not task.done()


def get_event_count(session_id: str) -> int:
    """Return number of buffered events for a session."""
    buf = _EXECUTIONS.get(session_id)
    return len(buf["events"]) if buf else 0


def cleanup(session_id: str) -> None:
    """Remove a session's execution buffer (after client confirms receipt)."""
    _EXECUTIONS.pop(session_id, None)
