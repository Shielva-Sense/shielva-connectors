"""Integration Builder — WebSocket routes for real-time execution + inline prompts.

Provides bidirectional WebSocket communication:
  - Server → Client: execution/auto-run progress events (same as SSE, but as JSON)
  - Client → Server: start_execution, start_auto_run, user_prompt, ping
"""

import asyncio
import contextlib
import json
from datetime import datetime
from typing import Any

import structlog
from bson import ObjectId
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from integration.core.config import settings
from integration.db.database import sessions_collection
from integration.schemas.models import SessionStatus, StepStatus
from integration.services import r2_service
from integration.services.codegen_service import auto_run_session, execute_plan
from integration.services.user_prompt_handler import handle_user_prompt

logger = structlog.get_logger(__name__)

ws_router = APIRouter()


# ── Shared connection registry ────────────────────────────────────────────────
class _SessionWsManager:
    """Global registry of active WebSocket connections per session.

    Allows session_routes (PATCH /steps/.../status) to broadcast step updates
    to all CMS clients currently watching a session — without a dedicated
    Electron→CMS WebSocket channel.
    """

    def __init__(self) -> None:
        self._connections: dict[str, set] = {}

    def register(self, session_id: str, ws: WebSocket) -> None:
        self._connections.setdefault(session_id, set()).add(ws)

    def unregister(self, session_id: str, ws: WebSocket) -> None:
        bucket = self._connections.get(session_id)
        if bucket:
            bucket.discard(ws)
            if not bucket:
                del self._connections[session_id]

    async def broadcast(self, session_id: str, message: dict[str, Any]) -> None:
        """Send *message* to every live WebSocket watching *session_id*."""
        dead: set = set()
        for ws in list(self._connections.get(session_id, [])):
            try:
                if _ws_is_open(ws):
                    await ws.send_json(message)
                else:
                    dead.add(ws)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.unregister(session_id, ws)


# Module-level singleton — imported by session_routes for PATCH broadcasts
ws_manager = _SessionWsManager()


def _parse_sse_event(sse_chunk: str) -> dict[str, Any] | None:
    """Parse an SSE string like 'event: X\\ndata: {...}\\n\\n' into a dict."""
    event_type = ""
    data_str = ""
    for line in sse_chunk.strip().split("\n"):
        if line.startswith("event: "):
            event_type = line[7:].strip()
        elif line.startswith("data: "):
            data_str = line[6:]
    if event_type and data_str:
        try:
            return {"type": event_type, "data": json.loads(data_str)}
        except json.JSONDecodeError:
            return {"type": event_type, "data": {"raw": data_str}}
    return None


def _ws_is_open(ws: WebSocket) -> bool:
    """Check if WebSocket is still open."""
    return ws.client_state == WebSocketState.CONNECTED


@ws_router.websocket("/ws/sessions/{session_id}/execute")
async def ws_execute(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for bidirectional execution streaming.

    Query params:
      - tenant_id (required): multi-tenant isolation

    Client messages:
      - { "type": "start_execution" }
      - { "type": "start_auto_run" }
      - { "type": "user_prompt", "prompt": "fix the import error" }
      - { "type": "ping" }

    Server messages:
      - All SSE events as { "type": "...", "data": {...} }
      - prompt_received, prompt_processing, prompt_llm_calling,
        prompt_file_updated, prompt_complete, prompt_error
      - pong, error, keepalive
    """
    # tenant_id query param is required only for a pre-accept sanity check.
    # The authoritative tenant_id and tenant_name are always read from the
    # session document after accept — never trusted from the client.
    if not websocket.query_params.get("tenant_id", ""):
        await websocket.close(code=4001, reason="tenant_id query param required")
        return

    # Origin validation
    origin = websocket.headers.get("origin", "")
    allowed_origins = settings.CORS_ORIGINS
    if origin and origin not in allowed_origins:
        logger.warning("ws.origin_rejected", origin=origin)
        await websocket.close(code=4003, reason="Origin not allowed")
        return

    await websocket.accept()

    # ── Resolve tenant_id + tenant_name from the session document ─────────────
    # The session is the authoritative source for both fields — they are stored
    # once at session-creation time from the gateway-injected headers and never
    # change.  We do not trust the client-supplied query params for scoping.
    # Fallback to query params only for legacy sessions (tenant_name was null
    # before the tenant_name fix landed).
    _sess_doc = None
    try:
        _sess_doc = await sessions_collection().find_one(
            {"_id": ObjectId(session_id)},
            {"tenant_id": 1, "tenant_name": 1, "app_id": 1},
        )
    except Exception as _e:
        logger.warning("ws.session_lookup_error", session_id=session_id, error=str(_e))

    if not _sess_doc:
        await websocket.close(code=4004, reason="Session not found")
        return

    tenant_id = (_sess_doc.get("tenant_id") or "").strip()
    tenant_name = (_sess_doc.get("tenant_name") or "").strip().lower()
    app_id = (_sess_doc.get("app_id") or "").strip()

    if not tenant_id:
        logger.warning("ws.session_missing_tenant_id", session_id=session_id)

    # tenant_name fallback for pre-fix sessions where it was stored as null
    if not tenant_name:
        tenant_name = websocket.query_params.get("tenant_name", "").strip().lower()
        if tenant_name:
            logger.info(
                "ws.tenant_name_from_query_param_fallback",
                session_id=session_id,
                tenant_name=tenant_name,
            )

    # Set R2 bucket context — priority: app_id bucket (preferred) → tenant_name (legacy fallback)
    if app_id:
        _app_bucket = r2_service.app_id_to_bucket(app_id)
        r2_service._app_bucket_ctx.set(_app_bucket)
        logger.info(
            "ws.bucket_ctx_set",
            session_id=session_id,
            bucket=_app_bucket,
            source="app_id",
        )
    if tenant_name:
        r2_service._tenant_bucket_ctx.set(tenant_name)
        if not app_id:
            logger.info(
                "ws.bucket_ctx_set",
                session_id=session_id,
                bucket=tenant_name,
                source="tenant_name",
            )
    if not app_id and not tenant_name:
        logger.warning(
            "ws.bucket_unresolved",
            session_id=session_id,
            note="session has no app_id and no tenant_name — r2_service uses R2_BUCKET_NAME env fallback",
        )

    logger.info("ws.connected", session_id=session_id, tenant_id=tenant_id)
    ws_manager.register(session_id, websocket)

    # Shared state
    phase = "idle"  # idle | executing | processing_prompt
    prompt_queue: asyncio.Queue[str] = asyncio.Queue()
    # Queue for outbound messages — decouples send from execution
    outbound_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    shutdown_event = asyncio.Event()
    # Track the execution task so we can wait for it on disconnect
    execution_task: asyncio.Task | None = None

    async def send_event(event_type: str, data: dict[str, Any]):
        """Queue a JSON message for sending to the client."""
        if not shutdown_event.is_set():
            await outbound_queue.put({"type": event_type, "data": data})

    async def outbound_sender():
        """Dedicated task to send queued messages over WebSocket.

        Drains the queue continuously. On send failure, sets shutdown.
        """
        try:
            while not shutdown_event.is_set():
                try:
                    msg = await asyncio.wait_for(outbound_queue.get(), timeout=1.0)
                    if _ws_is_open(websocket):
                        await websocket.send_json(msg)
                except TimeoutError:
                    continue
                except Exception as exc:
                    logger.warning("ws.send_error", error=str(exc))
                    break
        except asyncio.CancelledError:
            # Drain any remaining messages before exiting
            while not outbound_queue.empty():
                try:
                    msg = outbound_queue.get_nowait()
                    if _ws_is_open(websocket):
                        await websocket.send_json(msg)
                except Exception:
                    break

    async def keepalive():
        """Send frequent keepalive messages through the outbound queue.

        IMPORTANT: Must use outbound_queue (not websocket.send_json directly)
        to avoid concurrent writes with outbound_sender — two coroutines
        writing to the same WebSocket simultaneously causes protocol errors.

        Sends every 3 seconds to keep browser connection alive during long-running
        operations like pytest (which may not produce output for several seconds).
        """
        try:
            while not shutdown_event.is_set():
                await asyncio.sleep(3)  # More frequent keepalives during execution
                if not _ws_is_open(websocket):
                    break
                try:
                    await send_event(
                        "keepalive",
                        {"phase": phase, "timestamp": asyncio.get_event_loop().time()},
                    )
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def stream_sse_generator(generator):
        """Consume an existing SSE async generator and forward events via outbound queue."""
        async for sse_chunk in generator:
            if shutdown_event.is_set():
                break
            msg = _parse_sse_event(sse_chunk)
            if msg:
                await send_event(msg["type"], msg["data"])

    async def monitor_running_execution(session_oid: ObjectId):
        """Attach to an already-running execution by polling MongoDB.

        Used when a WS reconnects mid-execution (e.g. after server reload).
        Replays step completions from DB and waits for session to reach terminal state.

        If the session stays 'executing' with no step progress for 5 minutes
        (server crash with no startup sweep, or stuck code path), this monitor
        marks it 'failed' so the frontend can offer a re-run instead of hanging.
        """
        nonlocal phase
        phase = "executing"
        seen_completed: set[int] = set()
        _MONITOR_TIMEOUT_S = 300  # 5 min with no progress → treat as crashed
        _no_progress_since = asyncio.get_event_loop().time()

        # Fetch initial doc to send a proper execution_start so the frontend
        # clears any stale "failed" status from the previous session restore.
        init_doc = await sessions_collection().find_one({"_id": session_oid})
        if init_doc:
            init_steps = init_doc.get("plan", {}).get("steps", [])
            await send_event(
                "execution_start",
                {
                    "session_id": str(session_oid),
                    "step_count": len(init_steps),
                    "service": init_doc.get("service", "service"),
                    "reconnected": True,
                },
            )

        await send_event(
            "step_log",
            {
                "step_index": -1,
                "level": "info",
                "message": "Reconnected — attaching to running execution...",
            },
        )
        try:
            while not shutdown_event.is_set():
                doc = await sessions_collection().find_one({"_id": session_oid})
                if not doc:
                    break
                steps = doc.get("plan", {}).get("steps", [])
                progress_made = False
                for i, step in enumerate(steps):
                    if step.get("status") == StepStatus.COMPLETED.value and i not in seen_completed:
                        seen_completed.add(i)
                        progress_made = True
                        await send_event(
                            "step_complete",
                            {
                                "step_index": i,
                                "title": step.get("title", f"Step {i + 1}"),
                                "status": "pass",
                                "duration_ms": 0,
                            },
                        )
                    elif step.get("status") == StepStatus.FAILED.value and i not in seen_completed:
                        seen_completed.add(i)
                        progress_made = True
                        await send_event(
                            "step_complete",
                            {
                                "step_index": i,
                                "title": step.get("title", f"Step {i + 1}"),
                                "status": "fail",
                                "duration_ms": 0,
                            },
                        )
                if progress_made:
                    _no_progress_since = asyncio.get_event_loop().time()

                session_status = doc.get("status", "")
                if session_status in (
                    SessionStatus.COMPLETED.value,
                    SessionStatus.FAILED.value,
                ):
                    await send_event(
                        "execution_complete",
                        {
                            "status": session_status,
                            "message": f"Execution {session_status}",
                        },
                    )
                    break

                # Self-heal: if execution is still 'executing' but nothing has moved
                # for _MONITOR_TIMEOUT_S, the server process that owned it is dead.
                elapsed = asyncio.get_event_loop().time() - _no_progress_since
                if elapsed > _MONITOR_TIMEOUT_S:
                    logger.warning(
                        "ws.monitor_timeout",
                        session_id=str(session_oid),
                        elapsed_s=int(elapsed),
                    )
                    # Mark session failed in DB so future reconnects don't loop here again
                    try:
                        from datetime import datetime as _dt

                        await sessions_collection().update_one(
                            {
                                "_id": session_oid,
                                "status": SessionStatus.EXECUTING.value,
                            },
                            {
                                "$set": {
                                    "status": SessionStatus.FAILED.value,
                                    "error": "Execution timed out — server may have restarted. Please re-run.",
                                    "updated_at": _dt.utcnow(),
                                }
                            },
                        )
                    except Exception:
                        pass
                    await send_event(
                        "step_log",
                        {
                            "step_index": -1,
                            "level": "error",
                            "message": "⚠ Execution appears to have crashed (no progress for 5 min). Use Re-Execute to restart.",
                        },
                    )
                    await send_event(
                        "execution_complete",
                        {
                            "status": "failed",
                            "message": "Execution timed out — server crashed. Please re-run.",
                        },
                    )
                    break

                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass
        finally:
            phase = "idle"
            await send_event("execution_done", {"message": "Monitor finished"})

    async def run_execution(from_step_index: int = 0, force_restart: bool = False):
        """Execute plan and stream events."""
        nonlocal phase
        phase = "executing"
        try:
            gen = execute_plan(
                session_id,
                tenant_id,
                from_step_index=from_step_index,
                force_restart=force_restart,
            )
            await stream_sse_generator(gen)
        except asyncio.CancelledError:
            logger.info("ws.execution_cancelled", session_id=session_id)
        except Exception as exc:
            await send_event("error", {"message": f"Execution error: {exc}"})
            logger.error("ws.execution_error", session_id=session_id, error=str(exc))
        finally:
            phase = "idle"
            # Signal execution done so frontend can update UI
            await send_event("execution_done", {"message": "Execution task finished"})
            # Process any queued prompts
            while not prompt_queue.empty():
                p = await prompt_queue.get()
                await process_user_prompt(p)

    async def run_auto_run():
        """Auto-run session and stream events."""
        nonlocal phase
        phase = "executing"
        try:
            gen = auto_run_session(session_id, tenant_id)
            await stream_sse_generator(gen)
        except asyncio.CancelledError:
            logger.info("ws.auto_run_cancelled", session_id=session_id)
        except Exception as exc:
            await send_event("error", {"message": f"Auto-run error: {exc}"})
            logger.error("ws.auto_run_error", session_id=session_id, error=str(exc))
        finally:
            phase = "idle"
            await send_event("execution_done", {"message": "Auto-run task finished"})
            # Process any queued prompts
            while not prompt_queue.empty():
                p = await prompt_queue.get()
                await process_user_prompt(p)

    async def process_user_prompt(prompt: str):
        """Process a user prompt to modify code."""
        nonlocal phase
        phase = "processing_prompt"
        try:
            await handle_user_prompt(session_id, tenant_id, prompt, send_event)
        except Exception as exc:
            await send_event("prompt_error", {"message": f"Prompt processing error: {exc}"})
            logger.error("ws.prompt_error", session_id=session_id, error=str(exc))
        finally:
            phase = "idle"

    # Queue for inbound messages — decouples receive_text() from message dispatch.
    # receive_text() runs in a dedicated task that is NEVER cancelled (cancelling
    # Starlette's receive_text() mid-flight corrupts WebSocket state and closes it).
    inbound_queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def inbound_receiver():
        """Dedicated task that calls receive_text() and pushes results to inbound_queue.

        Never cancelled directly — sets None sentinel on disconnect so the listener
        loop can exit cleanly.
        """
        try:
            while not shutdown_event.is_set():
                try:
                    raw = await websocket.receive_text()
                    await inbound_queue.put(raw)
                except WebSocketDisconnect:
                    logger.info("ws.disconnected", session_id=session_id)
                    break
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            await inbound_queue.put(None)  # sentinel — tell listener to exit

    async def listen_for_messages():
        """Dispatch incoming client messages from inbound_queue.

        Uses a short timeout on queue.get() so shutdown_event is checked regularly.
        The actual receive_text() runs in inbound_receiver and is never cancelled,
        avoiding Starlette WebSocket state corruption.
        """
        nonlocal phase, execution_task
        try:
            while not shutdown_event.is_set():
                try:
                    raw = await asyncio.wait_for(inbound_queue.get(), timeout=5.0)
                except TimeoutError:
                    # Queue empty for 5s — just check shutdown and loop
                    continue

                if raw is None:
                    # Disconnect sentinel from inbound_receiver
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await send_event("error", {"message": "Invalid JSON"})
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "ping":
                    await send_event("pong", {})

                elif msg_type == "start_execution":
                    if phase == "idle":
                        from_step = int(msg.get("from_step_index", 0))
                        force_restart = bool(msg.get("force_restart", False))

                        if force_restart:
                            # Re-Execute: always run fresh — bypass already_executing monitor
                            execution_task = asyncio.create_task(run_execution(from_step_index=0, force_restart=True))
                        else:
                            # Check if this session is already executing (e.g. WS reconnect after drop)
                            try:
                                oid = ObjectId(session_id)
                                doc = await sessions_collection().find_one({"_id": oid})
                                already_executing = doc and doc.get("status") == SessionStatus.EXECUTING.value
                            except Exception:
                                already_executing = False
                            if already_executing and from_step == 0:
                                # Attach via polling — don't start a duplicate execution
                                execution_task = asyncio.create_task(monitor_running_execution(oid))
                            else:
                                execution_task = asyncio.create_task(run_execution(from_step_index=from_step))
                    else:
                        await send_event(
                            "error",
                            {"message": f"Cannot start execution — currently {phase}"},
                        )

                elif msg_type == "stop_execution":
                    # Immediately cancel any running execution/auto-run task
                    if execution_task and not execution_task.done():
                        execution_task.cancel()
                        # Wait briefly for the task to acknowledge the cancellation.
                        # NOTE: do NOT use asyncio.shield() here — shield() protects a
                        # task from being cancelled by its awaiter, which is the opposite
                        # of what we want. We want the CancelledError to propagate into
                        # the task so it can clean up (kill subprocesses, close streams).
                        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                            await asyncio.wait_for(execution_task, timeout=5.0)
                        # Mark session as failed in MongoDB so reconnect doesn't re-attach
                        try:
                            oid = ObjectId(session_id)
                            await sessions_collection().update_one(
                                {"_id": oid},
                                {
                                    "$set": {
                                        "status": SessionStatus.FAILED.value,
                                        "updated_at": datetime.utcnow(),
                                    }
                                },
                            )
                        except Exception:
                            pass
                        # Drain any queued prompts the user no longer wants auto-processed
                        while not prompt_queue.empty():
                            try:
                                prompt_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                        await send_event(
                            "execution_complete",
                            {
                                "status": "failed",
                                "message": "Execution stopped by user",
                                "file_count": 0,
                            },
                        )
                        logger.info("ws.stop_requested", session_id=session_id)
                    else:
                        await send_event(
                            "step_log",
                            {
                                "step_index": -1,
                                "level": "info",
                                "message": "No active execution to stop",
                            },
                        )

                elif msg_type == "start_auto_run":
                    if phase == "idle":
                        execution_task = asyncio.create_task(run_auto_run())
                    else:
                        await send_event(
                            "error",
                            {"message": f"Cannot start auto-run — currently {phase}"},
                        )

                elif msg_type == "user_prompt":
                    prompt_text = msg.get("prompt", "").strip()
                    if not prompt_text:
                        await send_event("prompt_error", {"message": "Empty prompt"})
                        continue

                    await send_event("prompt_received", {"prompt": prompt_text})

                    if phase == "idle":
                        execution_task = asyncio.create_task(process_user_prompt(prompt_text))
                    else:
                        # Queue for later — include prompt text so frontend can offer "Stop & Run"
                        await prompt_queue.put(prompt_text)
                        await send_event(
                            "prompt_queued",
                            {
                                "message": "Prompt queued — execution is running. Use Stop to cancel and run now.",
                                "prompt": prompt_text,
                            },
                        )

                else:
                    await send_event("error", {"message": f"Unknown message type: {msg_type}"})

        except Exception as exc:
            logger.error("ws.listener_error", error=str(exc))

    # Run all tasks concurrently
    sender_task = asyncio.create_task(outbound_sender())
    keepalive_task = asyncio.create_task(keepalive())
    receiver_task = asyncio.create_task(inbound_receiver())

    try:
        await listen_for_messages()
    finally:
        # Wait for any running execution task to finish (give it time to complete)
        if execution_task and not execution_task.done():
            logger.info("ws.waiting_for_execution", session_id=session_id)
            try:
                # Wait up to 5s on disconnect — if execution is still running after that,
                # cancel it. Users should use stop_execution to stop gracefully.
                await asyncio.wait_for(execution_task, timeout=5)
            except TimeoutError:
                logger.warning("ws.execution_timeout", session_id=session_id)
                execution_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await execution_task
            except Exception:
                pass

        shutdown_event.set()

        # Give sender a moment to drain the outbound queue
        await asyncio.sleep(0.5)

        receiver_task.cancel()
        sender_task.cancel()
        keepalive_task.cancel()
        for task in (receiver_task, sender_task, keepalive_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if _ws_is_open(websocket):
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)

        ws_manager.unregister(session_id, websocket)
        logger.info("ws.cleanup", session_id=session_id)
