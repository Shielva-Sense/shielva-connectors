"""System / environment status routes.

Endpoints:
  GET /api/v3/system/env-status          — snapshot of Python env health
  GET /api/v3/system/env-setup/stream    — SSE stream for venv setup progress
"""

import asyncio
import json
import shutil
import subprocess
import sys
from asyncio import Queue
from pathlib import Path
from typing import AsyncIterator

import structlog
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = structlog.get_logger(__name__)

system_router = APIRouter(tags=["system"])


# ── helpers ──────────────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> str:
    return f"data: {json.dumps({'type': event, **data})}\n\n"


def _python313_path() -> str | None:
    """Return path to python3.13 if available, else None."""
    p = shutil.which("python3.13") or "/opt/homebrew/bin/python3.13"
    return p if Path(p).exists() else None


def _pip_available(python: str) -> bool:
    """Check pip is importable for the given Python executable."""
    result = subprocess.run(
        [python, "-m", "pip", "--version"],
        capture_output=True, text=True, timeout=10
    )
    return result.returncode == 0


def _pkg_version(python: str, pkg: str) -> str | None:
    """Return installed version of a package in the given Python env, or None."""
    result = subprocess.run(
        [python, "-c", f"import importlib.metadata; print(importlib.metadata.version('{pkg}'))"],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip() if result.returncode == 0 else None


# ── /system/env-status ───────────────────────────────────────────────────────

@system_router.get("/system/env-status")
async def get_env_status():
    """Snapshot of Python environment health — used by the Settings panel."""
    try:
        from integration.services.shared_venv import VENV_DIR, get_venv_python
    except ImportError:
        return {"ok": False, "error": "shared_venv module not available"}

    py313 = _python313_path()
    venv_exists = (VENV_DIR / "bin" / "python").exists()
    marker_ok = (VENV_DIR / ".common_deps_installed").exists()
    sdk_ok = (VENV_DIR / ".sdk_installed").exists()

    venv_python = str(VENV_DIR / "bin" / "python") if venv_exists else None
    pip_ok = _pip_available(venv_python) if venv_python else False

    pydantic_ver = _pkg_version(venv_python, "pydantic") if venv_python else None
    httpx_ver = _pkg_version(venv_python, "httpx") if venv_python else None

    python_ver = None
    if venv_python:
        r = subprocess.run([venv_python, "--version"], capture_output=True, text=True, timeout=5)
        python_ver = r.stdout.strip() or r.stderr.strip()

    checks = {
        "python313_found": py313 is not None,
        "python313_path": py313,
        "venv_exists": venv_exists,
        "venv_path": str(VENV_DIR),
        "pip_available": pip_ok,
        "common_deps_installed": marker_ok,
        "sdk_installed": sdk_ok,
        "venv_python_version": python_ver,
        "pydantic_version": pydantic_ver,
        "httpx_version": httpx_ver,
    }

    overall_ok = venv_exists and pip_ok and marker_ok and sdk_ok
    return {"ok": overall_ok, "checks": checks}


# ── /system/env-uninstall ────────────────────────────────────────────────────

@system_router.post("/system/env-uninstall")
async def uninstall_env():
    """Delete the shared venv directory so it can be rebuilt from scratch."""
    try:
        from integration.services.shared_venv import VENV_DIR
    except ImportError:
        return {"ok": False, "error": "shared_venv module not available"}

    if not VENV_DIR.exists():
        return {"ok": True, "message": "Virtual environment does not exist — nothing to remove"}

    try:
        import shutil as _shutil
        _shutil.rmtree(str(VENV_DIR))
        logger.info("shared_venv.uninstalled", path=str(VENV_DIR))
        return {"ok": True, "message": f"Removed {VENV_DIR.name}"}
    except Exception as exc:
        logger.error("shared_venv.uninstall_failed", error=str(exc))
        return {"ok": False, "error": str(exc)}


# ── /system/env-setup/stream ─────────────────────────────────────────────────

@system_router.get("/system/env-setup/stream")
async def stream_env_setup():
    """SSE stream that runs (or re-runs) the shared venv setup and emits progress."""

    queue: Queue[str] = Queue()

    async def _run_setup():
        """Run venv setup steps in a thread, feeding events into the queue."""
        try:
            from integration.services.shared_venv import VENV_DIR, COMMON_DEPS
        except ImportError:
            await queue.put(_sse("error", {"message": "shared_venv module not available"}))
            await queue.put(_sse("done", {"ok": False}))
            return

        import shutil as _shutil

        # Step 1 — check python3.13
        py313 = _shutil.which("python3.13") or "/opt/homebrew/bin/python3.13"
        if not Path(py313).exists():
            py313 = sys.executable  # fallback
            await queue.put(_sse("step", {
                "step": "python_check",
                "status": "warn",
                "message": f"python3.13 not found — using {py313}",
            }))
        else:
            await queue.put(_sse("step", {
                "step": "python_check",
                "status": "ok",
                "message": f"Found python3.13 at {py313}",
            }))
        await asyncio.sleep(0)

        # Step 2 — create venv
        venv_python = VENV_DIR / "bin" / "python"
        if venv_python.exists():
            await queue.put(_sse("step", {
                "step": "create_venv",
                "status": "skip",
                "message": "Virtual environment already exists",
            }))
        else:
            await queue.put(_sse("step", {
                "step": "create_venv",
                "status": "running",
                "message": f"Creating Python virtual environment at {VENV_DIR.name}/",
            }))
            result = await asyncio.to_thread(
                subprocess.run,
                [py313, "-m", "venv", str(VENV_DIR)],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                await queue.put(_sse("step", {
                    "step": "create_venv",
                    "status": "error",
                    "message": f"Failed to create venv: {result.stderr[:200]}",
                }))
                await queue.put(_sse("done", {"ok": False}))
                return
            await queue.put(_sse("step", {
                "step": "create_venv",
                "status": "ok",
                "message": "Virtual environment created",
            }))

        await asyncio.sleep(0)

        # Step 3 — check pip
        venv_pip = VENV_DIR / "bin" / "pip"
        if not venv_pip.exists():
            await queue.put(_sse("step", {
                "step": "pip_check",
                "status": "error",
                "message": "pip not found in virtual environment",
            }))
            await queue.put(_sse("done", {"ok": False}))
            return
        await queue.put(_sse("step", {
            "step": "pip_check",
            "status": "ok",
            "message": "pip is available",
        }))
        await asyncio.sleep(0)

        # Step 4 — install common deps
        marker = VENV_DIR / ".common_deps_installed"
        if marker.exists():
            await queue.put(_sse("step", {
                "step": "common_deps",
                "status": "skip",
                "message": f"Common dependencies already installed ({len(COMMON_DEPS)} packages)",
            }))
        else:
            await queue.put(_sse("step", {
                "step": "common_deps",
                "status": "running",
                "message": f"Installing {len(COMMON_DEPS)} common packages (pydantic, httpx, google-auth…)",
            }))
            result = await asyncio.to_thread(
                subprocess.run,
                [str(venv_pip), "install", "--quiet"] + COMMON_DEPS,
                capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                await queue.put(_sse("step", {
                    "step": "common_deps",
                    "status": "error",
                    "message": f"Dependency install failed: {result.stderr[:300]}",
                }))
                await queue.put(_sse("done", {"ok": False}))
                return
            marker.write_text("installed")
            await queue.put(_sse("step", {
                "step": "common_deps",
                "status": "ok",
                "message": f"Installed {len(COMMON_DEPS)} common packages",
            }))

        await asyncio.sleep(0)

        # Step 5 — install SDK
        from integration.services.shared_venv import _CONNECTORS_ROOT
        sdk_marker = VENV_DIR / ".sdk_installed"
        if sdk_marker.exists():
            await queue.put(_sse("step", {
                "step": "sdk",
                "status": "skip",
                "message": "Shielva connector SDK already installed",
            }))
        else:
            await queue.put(_sse("step", {
                "step": "sdk",
                "status": "running",
                "message": "Installing Shielva connector SDK…",
            }))
            result = await asyncio.to_thread(
                subprocess.run,
                [str(venv_pip), "install", "--quiet", "-e", str(_CONNECTORS_ROOT)],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                await queue.put(_sse("step", {
                    "step": "sdk",
                    "status": "warn",
                    "message": f"SDK install warning: {result.stderr[:200]}",
                }))
            else:
                sdk_marker.write_text("installed")
                await queue.put(_sse("step", {
                    "step": "sdk",
                    "status": "ok",
                    "message": "Shielva connector SDK installed",
                }))

        await queue.put(_sse("done", {"ok": True}))

    asyncio.create_task(_run_setup())

    async def _stream() -> AsyncIterator[str]:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=120)
                yield msg
                if '"type": "done"' in msg or '"type":"done"' in msg:
                    break
            except asyncio.TimeoutError:
                yield _sse("error", {"message": "Setup timed out"})
                break

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
