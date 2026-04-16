"""Shared Python 3.13 virtual environment for connector dependency isolation.

All connector install_deps steps use this venv so:
  - Python version is always 3.13 (avoids Python 3.14 pydantic-core build failures)
  - Common dependencies are pre-installed once at startup (pydantic, httpx, structlog, etc.)
  - Connector-specific packages are installed on top without version conflicts
"""

import asyncio
import subprocess
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Fixed venv location — sibling of shielva-connectors package root
_CONNECTORS_ROOT = Path(__file__).resolve().parent.parent.parent
VENV_DIR = _CONNECTORS_ROOT / ".connector-venv"

# Python 3.13 binary (preferred; falls back to system python3)
_PYTHON_313 = "/opt/homebrew/bin/python3.13"

# Common dependencies pre-installed at startup — versions with Python 3.13 wheels
COMMON_DEPS = [
    "pydantic>=2.10",           # pydantic-core 2.27+ has Python 3.13 wheels
    "httpx>=0.27",
    "structlog>=24",
    "google-api-python-client>=2.140.0",
    "google-auth>=2.34.0",
    "google-auth-oauthlib>=1.2.1",
    "google-auth-httplib2>=0.2.0",
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-mock>=3.14",
    "pytest-timeout>=2.4",
    "pytest-cov>=4.1",
]


def get_venv_python() -> str:
    """Return path to the venv's Python executable."""
    venv_python = VENV_DIR / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    # Fallback to system python3.13 or sys.executable
    import shutil as _shutil
    py313 = _shutil.which("python3.13") or _PYTHON_313
    if Path(py313).exists():
        return py313
    return sys.executable


def _create_venv() -> bool:
    """Create the shared venv with Python 3.13 if it doesn't exist. Returns True if created."""
    if (VENV_DIR / "bin" / "python").exists():
        return False  # already exists

    import shutil as _shutil
    py313 = _shutil.which("python3.13") or _PYTHON_313
    if not Path(py313).exists():
        logger.warning("shared_venv.python313_not_found", fallback=sys.executable)
        py313 = sys.executable

    logger.info("shared_venv.creating", path=str(VENV_DIR), python=py313)
    result = subprocess.run(
        [py313, "-m", "venv", str(VENV_DIR)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error("shared_venv.create_failed", stderr=result.stderr[:300])
        return False
    logger.info("shared_venv.created")
    return True


def _install_common_deps() -> None:
    """Install common deps into the venv — skipped if already installed (marker file present)."""
    venv_pip = VENV_DIR / "bin" / "pip"
    if not venv_pip.exists():
        logger.error("shared_venv.pip_not_found", venv=str(VENV_DIR))
        return

    # Use a marker file so we only install once, not on every app restart
    marker = VENV_DIR / ".common_deps_installed"
    if marker.exists():
        logger.info("shared_venv.common_deps_already_installed")
        return

    logger.info("shared_venv.installing_common_deps", count=len(COMMON_DEPS))
    result = subprocess.run(
        [str(venv_pip), "install", "--quiet"] + COMMON_DEPS,
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        logger.error("shared_venv.common_deps_failed", stderr=result.stderr[:500])
    else:
        marker.write_text("installed")
        logger.info("shared_venv.common_deps_installed")


def setup_shared_venv() -> None:
    """Create shared venv and pre-install common deps. Call once at app startup (sync)."""
    try:
        _create_venv()
        _install_common_deps()

        # Also install the shielva-connectors SDK itself into the venv (once)
        sdk_path = _CONNECTORS_ROOT
        venv_pip = VENV_DIR / "bin" / "pip"
        sdk_marker = VENV_DIR / ".sdk_installed"
        if venv_pip.exists() and (sdk_path / "pyproject.toml").exists() and not sdk_marker.exists():
            result = subprocess.run(
                [str(venv_pip), "install", "--quiet", "-e", str(sdk_path)],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                logger.warning("shared_venv.sdk_install_failed", stderr=result.stderr[:300])
            else:
                sdk_marker.write_text("installed")
                logger.info("shared_venv.sdk_installed", path=str(sdk_path))
    except Exception as exc:
        logger.error("shared_venv.setup_error", error=str(exc))


async def setup_shared_venv_async() -> None:
    """Async wrapper for setup_shared_venv — use in FastAPI lifespan."""
    await asyncio.to_thread(setup_shared_venv)
