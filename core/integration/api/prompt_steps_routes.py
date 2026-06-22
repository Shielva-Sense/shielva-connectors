"""Integration Builder — Prompt Steps API routes.

Tracks per-session prompt step execution state in MongoDB.
Persists prompt_execution.json to R2 for audit/resume.

MongoDB collection: prompt_steps
R2 key: {collection}/{provider}/{service}/prompt_execution.json
"""

import asyncio
import os
import subprocess
import uuid
from datetime import datetime
from functools import partial
from typing import List, Optional

import structlog
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from integration.db.database import get_db
from integration.services import r2_service

logger = structlog.get_logger(__name__)

prompt_steps_router = APIRouter(prefix="/sessions", tags=["prompt-steps"])


# ── Helpers ────────────────────────────────────────────────────────────

def _col():
    return get_db()["prompt_steps"]


def _prompt_execution_key(provider: str, service_slug: str) -> str:
    return f"{r2_service._coll()}/{provider}/{service_slug}/prompt_execution.json"


async def _sync_r2(session_id: str, provider: str, service_slug: str) -> None:
    """Persist full prompt_execution.json to R2 (fire-and-forget)."""
    import json
    try:
        col = _col()
        docs = await col.find(
            {"session_id": session_id}, {"_id": 0}
        ).sort("created_at", 1).to_list(length=200)

        payload = json.dumps({
            "session_id": session_id,
            "provider": provider,
            "service_slug": service_slug,
            "steps": docs,
            "updated_at": datetime.utcnow().isoformat(),
        }, indent=2)

        key = _prompt_execution_key(provider, service_slug)
        if r2_service._use_local():
            r2_service._local_write(key, payload)
            return
        loop = asyncio.get_event_loop()
        client = r2_service._get_client()
        await loop.run_in_executor(
            None,
            partial(r2_service._sync_write, client, r2_service._get_bucket(),
                    key, payload, "application/json"),
        )
    except Exception as exc:
        logger.warning("prompt_steps.r2_sync_failed", error=str(exc))


# ── Pydantic models ────────────────────────────────────────────────────

class PromptStepUpsert(BaseModel):
    step_id: str
    prompt: str
    is_shell: bool = False
    status: str = "pending"


class PromptStepPatch(BaseModel):
    status: Optional[str] = None
    prompt: Optional[str] = None


class ValidateStepRequest(BaseModel):
    files: List[str] = []        # absolute paths to check existence + size
    run_tests: bool = False       # if True, run pytest in connector_dir
    connector_dir: str = ""       # working directory for pytest


# ── Routes ─────────────────────────────────────────────────────────────

@prompt_steps_router.get("/{session_id}/prompt-steps")
async def list_prompt_steps(session_id: str):
    """List all prompt steps for a session, ordered by creation time."""
    col = _col()
    cursor = col.find({"session_id": session_id}, {"_id": 0}).sort("created_at", 1)
    docs = await cursor.to_list(length=200)
    return {"steps": docs}


@prompt_steps_router.post("/{session_id}/prompt-steps")
async def upsert_prompt_step(
    session_id: str,
    body: PromptStepUpsert,
    x_tenant_id: str = Header("", alias="X-Tenant-ID"),
    x_provider: str = Header("", alias="X-Provider"),
    x_service: str = Header("", alias="X-Service"),
):
    """Upsert a prompt step. Returns existing record if already created (idempotent)."""
    col = _col()
    now = datetime.utcnow().isoformat()

    existing = await col.find_one(
        {"session_id": session_id, "step_id": body.step_id},
        {"_id": 0},
    )
    if existing:
        return {"step": existing, "created": False}

    doc = {
        "session_id": session_id,
        "step_id": body.step_id,
        "prompt_step_id": str(uuid.uuid4()),
        "status": body.status,
        "prompt": body.prompt,
        "is_shell": body.is_shell,
        "created_at": now,
        "updated_at": now,
    }
    await col.insert_one({**doc, "_id": doc["prompt_step_id"]})

    logger.info("prompt_step.created",
                session_id=session_id, step_id=body.step_id,
                prompt_step_id=doc["prompt_step_id"])

    if x_provider and x_service:
        asyncio.create_task(_sync_r2(session_id, x_provider, x_service))

    return {"step": doc, "created": True}


@prompt_steps_router.patch("/{session_id}/prompt-steps/{step_id}")
async def patch_prompt_step(
    session_id: str,
    step_id: str,
    body: PromptStepPatch,
    x_tenant_id: str = Header("", alias="X-Tenant-ID"),
    x_provider: str = Header("", alias="X-Provider"),
    x_service: str = Header("", alias="X-Service"),
):
    """Update status and/or prompt of a prompt step."""
    col = _col()
    update: dict = {"updated_at": datetime.utcnow().isoformat()}
    if body.status is not None:
        update["status"] = body.status
    if body.prompt is not None:
        update["prompt"] = body.prompt

    result = await col.find_one_and_update(
        {"session_id": session_id, "step_id": step_id},
        {"$set": update},
        return_document=True,
        projection={"_id": 0},
    )
    if not result:
        raise HTTPException(status_code=404, detail=f"Prompt step not found: {step_id}")

    logger.info("prompt_step.patched",
                session_id=session_id, step_id=step_id, fields=list(update.keys()))

    if x_provider and x_service:
        asyncio.create_task(_sync_r2(session_id, x_provider, x_service))

    return {"step": result}


@prompt_steps_router.post("/{session_id}/prompt-steps/{step_id}/rerun")
async def rerun_prompt_step(
    session_id: str,
    step_id: str,
    x_tenant_id: str = Header("", alias="X-Tenant-ID"),
):
    """Reset a prompt step to pending so it can be re-executed."""
    col = _col()
    result = await col.find_one_and_update(
        {"session_id": session_id, "step_id": step_id},
        {"$set": {"status": "pending", "updated_at": datetime.utcnow().isoformat()}},
        return_document=True,
        projection={"_id": 0},
    )
    if not result:
        raise HTTPException(status_code=404, detail=f"Prompt step not found: {step_id}")

    logger.info("prompt_step.rerun", session_id=session_id, step_id=step_id)
    return {"step": result}


@prompt_steps_router.post("/{session_id}/validate-step/{step_id}")
async def validate_prompt_step(
    session_id: str,
    step_id: str,
    body: ValidateStepRequest,
):
    """Validate whether a step's outputs already exist and are complete.

    - Checks each file in `body.files` for existence and non-zero size.
    - If `body.run_tests` is True, runs pytest in `body.connector_dir` and
      returns pass/fail based on the exit code.

    Returns:
        { "valid": bool, "reason": str }
    """
    # ── File validation ────────────────────────────────────────────────
    missing: list[str] = []
    for path in body.files:
        if not path:
            continue
        if not os.path.isfile(path) or os.path.getsize(path) < 50:
            missing.append(os.path.basename(path))

    if missing:
        return {
            "valid": False,
            "reason": f"Missing or empty: {', '.join(missing)}",
        }

    # ── Test validation ────────────────────────────────────────────────
    if body.run_tests and body.connector_dir:
        loop = asyncio.get_event_loop()

        def _run_pytest():
            try:
                result = subprocess.run(
                    ["python", "-m", "pytest", "tests/", "-v", "--tb=short", "-q"],
                    cwd=body.connector_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                passed = result.returncode == 0
                output = (result.stdout + result.stderr).strip()[-2000:]  # last 2KB
                return passed, output
            except Exception as exc:
                return False, str(exc)

        passed, output = await loop.run_in_executor(None, _run_pytest)
        if not passed:
            return {"valid": False, "reason": f"Tests failed:\n{output}"}
        return {"valid": True, "reason": f"Tests passed:\n{output}"}

    # All files present and no test run needed
    return {"valid": True, "reason": "All output files present"}
