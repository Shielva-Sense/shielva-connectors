"""
Shielva Connectors - Gateway Service
Central service for managing all connectors
"""
from fastapi import FastAPI, HTTPException, Request, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any, List, Optional
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
import json
import ast
import socket
import asyncio
import structlog
import uvicorn
import sys
import os

# Load core/.env into the process environment (MASTER_KEY etc.) before any
# service singleton reads os.getenv. override=False so server.sh-exported vars win.
from dotenv import load_dotenv
load_dotenv(override=False)

from services import credential_manager
from services.connector_store import connector_store

logger = structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger(__name__)


# ===== Data Models =====

class ConnectorInstallRequest(BaseModel):
    """Request to install a connector"""
    connector_type: str
    config: Dict[str, Any]


class ConnectorInstallResponse(BaseModel):
    """Response from connector installation"""
    connector_id: str
    connector_type: str
    status: str
    oauth_url: Optional[str] = None


class OAuthCallbackRequest(BaseModel):
    """OAuth callback data"""
    code: str
    state: Optional[str] = None


class CredentialRequest(BaseModel):
    """Request to store credentials"""
    credentials: Dict[str, Any]


class SyncRequest(BaseModel):
    """Request to sync a connector"""
    full_sync: bool = False
    kb_id: Optional[str] = None
    webhook_url: Optional[str] = None


class SyncResponse(BaseModel):
    """Response from sync request"""
    job_id: str
    status: str
    message: str


class ConnectorStatusResponse(BaseModel):
    """Connector status response"""
    connector_id: str
    connector_type: str
    health: str
    auth_status: str
    last_sync: Optional[datetime] = None
    documents_indexed: int = 0
    error: Optional[str] = None


# ===== Connector Registry =====

# Connector registry.
#
# The legacy hardcoded vendor connectors (Slack, Notion, GDrive, Confluence, Jira,
# Salesforce, GitHub, SharePoint, Zendesk, HubSpot) have been removed. Connectors are
# now authored exclusively via the Integration Builder (see ./integration), which
# generates BaseConnector subclasses into ./generated_connectors and registers them
# here at runtime through _load_generated_connectors(). This dict starts empty and is
# populated dynamically on startup and on each install/reload.
CONNECTOR_CLASSES = {}

# ── Deploy queue (multi-install) ─────────────────────────────────────────────
# `/connectors/deploy` returns immediately with a job_id. A small worker pool
# drains the queue and runs the real install (load → instantiate → install →
# initialize → health_check) per job. Clients poll `/connectors/deploy/jobs/{id}`
# until status == completed | failed. This stops one slow install from blocking
# the others (and from holding the request long enough for the browser to
# time-out and report "failed" when the work actually succeeded).
import uuid as _uuid_mod
import time as _time_mod
DEPLOY_WORKER_COUNT = int(os.getenv("DEPLOY_WORKER_COUNT", "4"))
DEPLOY_JOB_TTL_S = int(os.getenv("DEPLOY_JOB_TTL_S", "1800"))  # 30 min — long enough for SAD to poll
# Top-level timeout for ONE install pipeline. Caps the worst-case time a worker
# can be blocked on a single connector — protects the queue from a single
# misbehaving connector.install() that never returns. Per-step timeouts
# (initialize=8s, health_check=10s) still apply inside the pipeline; this is
# the absolute upper bound. The loader holds _LOAD_LOCK only inside this
# bound, so a hung loader can never wedge every other worker forever.
DEPLOY_PIPELINE_TIMEOUT_S = float(os.getenv("DEPLOY_PIPELINE_TIMEOUT_S", "120"))
DEPLOY_LOAD_TIMEOUT_S = float(os.getenv("DEPLOY_LOAD_TIMEOUT_S", "60"))
_DEPLOY_JOBS: Dict[str, Dict[str, Any]] = {}
_DEPLOY_QUEUE: Optional[asyncio.Queue] = None
_DEPLOY_WORKERS_STARTED = False
# Serialise `_load_generated_connectors()` — it does heavy sync disk + sys.modules
# mutation, and two workers running it interleaved on the event loop would step on
# each other's sys.path/sys.modules state.
_LOAD_LOCK: Optional[asyncio.Lock] = None


def _job(job_id: str) -> Dict[str, Any]:
    """Return or create the in-memory record for a deploy job.

    The `_done_event` is an `asyncio.Event` set by the worker the moment the
    job reaches a terminal state. The SSE endpoint awaits it so the client
    learns about completion without polling — single network round-trip,
    zero CPU spin.
    """
    if job_id not in _DEPLOY_JOBS:
        _DEPLOY_JOBS[job_id] = {
            "status": "queued",
            "queued_at": _time_mod.time(),
            "result": None,
            "error": None,
            "code": None,
            "_done_event": asyncio.Event(),
        }
    return _DEPLOY_JOBS[job_id]


def _serialise_job(job_id: str, job: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-safe projection of a deploy job — drops the internal asyncio.Event."""
    return {k: v for k, v in job.items() if not k.startswith("_")} | {"job_id": job_id}


def _ensure_deploy_runtime() -> None:
    """Lazy-init the queue/lock/worker pool on first /deploy request.

    Done lazily (not at import time) because asyncio primitives bind to the
    running event loop, and uvicorn hasn't started it yet at import.
    """
    global _DEPLOY_QUEUE, _LOAD_LOCK, _DEPLOY_WORKERS_STARTED
    if _DEPLOY_WORKERS_STARTED:
        return
    if _DEPLOY_QUEUE is None:
        _DEPLOY_QUEUE = asyncio.Queue()
    if _LOAD_LOCK is None:
        _LOAD_LOCK = asyncio.Lock()
    for i in range(DEPLOY_WORKER_COUNT):
        asyncio.create_task(_deploy_worker(i))
    asyncio.create_task(_deploy_jobs_gc())
    _DEPLOY_WORKERS_STARTED = True
    logger.info("deploy.workers_started", count=DEPLOY_WORKER_COUNT)


async def _deploy_jobs_gc() -> None:
    """Drop completed/failed jobs older than DEPLOY_JOB_TTL_S so the dict
    doesn't grow unbounded. Runs once a minute."""
    while True:
        try:
            await asyncio.sleep(60)
            now = _time_mod.time()
            stale = [
                jid for jid, j in _DEPLOY_JOBS.items()
                if j.get("status") in ("completed", "failed")
                and (now - (j.get("finished_at") or j.get("queued_at") or now)) > DEPLOY_JOB_TTL_S
            ]
            for jid in stale:
                _DEPLOY_JOBS.pop(jid, None)
            if stale:
                logger.info("deploy.jobs_gc", evicted=len(stale))
        except Exception as e:
            logger.warning("deploy.jobs_gc_failed", error=str(e))


async def _deploy_worker(worker_id: int) -> None:
    """Drain the deploy queue forever, running each job to completion."""
    assert _DEPLOY_QUEUE is not None
    while True:
        try:
            job_id, body, tenant_id = await _DEPLOY_QUEUE.get()
        except Exception as e:
            logger.warning("deploy.worker_queue_get_failed", worker=worker_id, error=str(e))
            continue
        job = _job(job_id)
        job["status"] = "running"
        job["started_at"] = _time_mod.time()
        job["worker"] = worker_id
        try:
            # Hard cap on a single install. If it blows, this worker is freed
            # immediately to pick up the next job rather than being held forever
            # by a connector whose install() / health_check() got stuck (e.g.
            # an upstream API that never returns and a missing timeout in the
            # connector's own HTTP client). The job is marked failed below.
            result = await asyncio.wait_for(
                _run_deploy_pipeline(body, tenant_id),
                timeout=DEPLOY_PIPELINE_TIMEOUT_S,
            )
            job["status"] = "completed"
            job["result"] = result
            job["finished_at"] = _time_mod.time()
            logger.info(
                "deploy.job_completed",
                job_id=job_id, worker=worker_id,
                connector_id=result.get("connector_id"),
                duration_s=round(job["finished_at"] - job["started_at"], 2),
            )
        except asyncio.TimeoutError:
            job["status"] = "failed"
            job["error"] = (
                f"Install timed out after {int(DEPLOY_PIPELINE_TIMEOUT_S)}s — the connector's install/health-check "
                "did not complete in time. Other queued installs are unaffected."
            )
            job["code"] = 504
            job["finished_at"] = _time_mod.time()
            logger.warning("deploy.job_timed_out", job_id=job_id, worker=worker_id)
        except HTTPException as he:
            job["status"] = "failed"
            job["error"] = he.detail
            job["code"] = he.status_code
            job["finished_at"] = _time_mod.time()
            logger.warning("deploy.job_failed", job_id=job_id, worker=worker_id, code=he.status_code, error=he.detail)
        except Exception as e:
            job["status"] = "failed"
            job["error"] = str(e)
            job["code"] = 500
            job["finished_at"] = _time_mod.time()
            logger.exception("deploy.job_crashed", job_id=job_id, worker=worker_id)
        finally:
            # Wake any SSE listeners (or pollers via the wait_for path) — done
            # even on crash so the client doesn't wait forever for a job whose
            # pipeline raised an unhandled exception.
            _evt = job.get("_done_event")
            if _evt is not None:
                _evt.set()


def _resolve_connector_type(connector_type: str) -> str:
    """Normalize connector_type to the canonical key used in CONNECTOR_CLASSES.

    Handles short names (e.g. "gmail") and case variants by checking if the
    exact key exists first, then falling back to a suffix/prefix scan of
    loaded CONNECTOR_CLASSES keys.
    """
    if connector_type in CONNECTOR_CLASSES:
        return connector_type
    # Try case-insensitive exact match
    lower = connector_type.lower().replace("-", "_")
    for key in CONNECTOR_CLASSES:
        if key.lower() == lower:
            return key
    # Try suffix match: "gmail" should match "shielva_gmail_connector"
    for key in CONNECTOR_CLASSES:
        if key.lower().endswith(f"_{lower}_connector") or key.lower().endswith(f"_{lower}") or key.lower() == f"shielva_{lower}_connector":
            return key
    # Try if the submitted type is a substring of a registered key
    for key in CONNECTOR_CLASSES:
        if lower in key.lower():
            return key
    return connector_type  # unchanged — will fail the "not in" check later


def _find_connector_json(connector_type: str) -> dict | None:
    """Locate and parse connector.json for a given connector_type.

    Scans generated_connectors/{tenant_id}/{pkg_dir}/metadata/connector.json
    and returns the first match whose CONNECTOR_TYPE matches.
    Returns None if not found.
    """
    root = Path(os.getenv("GENERATED_CODE_DIR") or _DEFAULT_GENERATED_DIR).resolve()
    if not root.exists():
        return None
    for tenant_dir in root.iterdir():
        if not tenant_dir.is_dir():
            continue
        for pkg_dir in tenant_dir.iterdir():
            meta_file = pkg_dir / "metadata" / "connector.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text())
                if meta.get("connector_type") == connector_type:
                    return meta
            except Exception:
                continue
    return None


def _validate_required_fields(config: dict, install_fields: list) -> list[str]:
    """Return a list of human-readable error messages for missing/empty required fields."""
    errors = []
    for field in install_fields:
        if not field.get("required"):
            continue
        key = field.get("key", "")
        label = field.get("label", key)
        value = config.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"'{label}' is required.")
    return errors


# ===== Advanced-connector hardening (isolation + HA reload) =====
#
# Generated connectors are AI-authored Python executed IN-PROCESS in this gateway
# (importlib.exec_module). With no OS sandbox, one hostile/buggy connector could
# read another tenant's in-memory secrets, shell out, or hang the event loop —
# blast radius = every tenant's advanced connectors. We apply three pragmatic
# defense-in-depth layers without a subprocess rewrite (see
# docs/CONNECTOR_ISOLATION.md):
#   1. Static AST scan that BLOCKS sandbox-escape / shell-out patterns at load.
#   2. Wall-clock timeout around module import (hang-on-import DoS).
#   3. Wall-clock timeout + thread offload around method invocation (runaway loop
#      / event-loop starvation that would freeze the gateway for all tenants).
#
# This is HARDENING, not a true sandbox: a static scan cannot stop every native
# escape. For fully-untrusted code the documented next step is subprocess/
# container workers (tracked separately). The scan kills the obvious classes.

CONNECTOR_AST_SCAN = os.getenv("CONNECTOR_AST_SCAN", "enforce").lower()      # enforce | warn | off
CONNECTOR_IMPORT_TIMEOUT_S = float(os.getenv("CONNECTOR_IMPORT_TIMEOUT_S", "10"))
CONNECTOR_INVOKE_TIMEOUT_S = float(os.getenv("CONNECTOR_INVOKE_TIMEOUT_S", "30"))

# Canonical connector tree lives at <repo-root>/generated_connectors/{tenant_id}/{name}_connector/.
# Resolved from this file's location (…/shielva-connectors/core/gateway.py →
# repo root …/shielva-connectors) so it's correct regardless of the process working directory —
# previously it defaulted to a cwd-relative "generated_connectors", which is the NESTED dir and
# does NOT match where SAD sync pushes connectors. Override with GENERATED_CODE_DIR if needed.
_DEFAULT_GENERATED_DIR = str(Path(__file__).resolve().parent.parent / "generated_connectors")

# Modules an HTTP/API connector never legitimately needs — importing any is a
# hard block (process / host escape surface).
_BLOCKED_IMPORTS = frozenset({
    "subprocess", "ctypes", "multiprocessing", "pty", "fcntl",
    "resource", "signal", "mmap", "_thread",
})
# CPython namespace-escape attributes (e.g. ().__class__.__bases__[0].__subclasses__()).
# None appear in legitimate connector logic.
_BLOCKED_ATTRS = frozenset({
    "__subclasses__", "__globals__", "__bases__", "__mro__",
    "__builtins__", "__code__", "__closure__",
})
# Bare callables that execute arbitrary code.
_BLOCKED_CALLS = frozenset({"eval", "exec", "compile", "__import__"})
# os.<attr> shell-out / process-control calls (os itself stays allowed for os.environ).
_BLOCKED_OS_ATTRS = frozenset({
    "system", "popen", "fork", "forkpty", "kill", "killpg", "setuid", "setgid",
    "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp",
    "spawnv", "spawnve", "spawnl", "spawnlp",
})


def _scan_connector_source(path: Path) -> list[str]:
    """Static AST scan of one generated connector source file.

    Returns a list of human-readable BLOCK reasons; empty list = clean.
    Best-effort defense-in-depth — see the hardening header above.
    """
    findings: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [f"unparseable source ({exc})"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _BLOCKED_IMPORTS:
                    findings.append(f"imports blocked module '{alias.name}'")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _BLOCKED_IMPORTS:
                findings.append(f"imports from blocked module '{node.module}'")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _BLOCKED_CALLS:
                findings.append(f"calls '{fn.id}()'")
            elif isinstance(fn, ast.Attribute) and fn.attr in _BLOCKED_OS_ATTRS \
                    and isinstance(fn.value, ast.Name) and fn.value.id == "os":
                findings.append(f"calls 'os.{fn.attr}()'")
        elif isinstance(node, ast.Attribute) and node.attr in _BLOCKED_ATTRS:
            findings.append(f"accesses escape attribute '{node.attr}'")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value in _BLOCKED_ATTRS:
            findings.append(f"references escape-attribute string '{node.value}'")

    seen: set[str] = set()
    ordered: list[str] = []
    for f in findings:
        if f not in seen:
            seen.add(f)
            ordered.append(f)
    return ordered


def _scan_connector_package(pkg_dir: Path) -> list[str]:
    """Scan every .py in a generated connector package (connector.py + sub-pkgs).

    Returns BLOCK reasons prefixed with the offending file's relative path.
    """
    reasons: list[str] = []
    for py_file in sorted(pkg_dir.rglob("*.py")):
        for reason in _scan_connector_source(py_file):
            reasons.append(f"{py_file.relative_to(pkg_dir)}: {reason}")
    return reasons


def _exec_module_with_timeout(exec_fn, timeout: float, label: str) -> None:
    """Run a *synchronous* module exec under a wall-clock timeout.

    importlib's exec_module blocks; a connector that hangs at import (infinite
    loop or blocking network call at module scope) would otherwise stall startup
    or a hot-reload for every tenant. We run the exec in a daemon thread and stop
    WAITING after `timeout`s — the runaway thread is a daemon, so it cannot block
    the gateway and dies with the process.
    """
    import threading

    error_box: list[BaseException] = []

    def _run() -> None:
        try:
            exec_fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised to caller below
            error_box.append(exc)

    thread = threading.Thread(target=_run, daemon=True, name=f"connimport-{label}")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"module import exceeded {timeout}s (possible hang at module scope)")
    if error_box:
        raise error_box[0]


# Cluster-wide connector-reload fan-out (HA). Shared mutable state — the
# in-process CONNECTOR_CLASSES registry — must reconcile across every gateway
# replica, per the project HA rule. A deploy/reload on any pod publishes here;
# every pod's subscriber reloads from the (shared) generated_connectors volume.
CONNECTOR_RELOAD_CHANNEL = os.getenv("CONNECTOR_RELOAD_CHANNEL", "shielva:connectors:reload")
POD_ID = f"{socket.gethostname()}-{os.getpid()}"


async def _publish_connector_reload(connector_type: str = None) -> None:
    """Broadcast a reload event to all gateway pods (fire-and-forget)."""
    from services.redis_service import redis_service
    try:
        message = json.dumps({"action": "reload", "origin": POD_ID, "connector_type": connector_type})
        subscribers = await redis_service.publish(CONNECTOR_RELOAD_CHANNEL, message)
        logger.info(
            "connector_reload.published",
            channel=CONNECTOR_RELOAD_CHANNEL, subscribers=subscribers,
            pod=POD_ID, connector_type=connector_type,
        )
    except Exception as exc:
        # Local reload already succeeded; cross-pod fan-out is best-effort.
        logger.warning("connector_reload.publish_failed", error=str(exc)[:200], pod=POD_ID)


async def _connector_reload_subscriber() -> None:
    """Subscribe to the cluster-wide reload channel and reload on every event.

    HA: a connector deploy/update on ANY pod publishes a reload; every other pod
    reloads its CONNECTOR_CLASSES from the shared generated_connectors volume, so
    no pod 404s a new connector or serves stale code. Assumes generated_connectors
    is a shared (RWX) volume or that each pod has pulled the same code — documented
    in docs/CONNECTOR_ISOLATION.md.
    """
    from services.redis_service import redis_service

    await redis_service.connect()
    if not redis_service.client:
        logger.warning("connector_reload_subscriber.no_redis — single-pod reload only", pod=POD_ID)
        return

    pubsub = redis_service.client.pubsub()
    await pubsub.subscribe(CONNECTOR_RELOAD_CHANNEL)
    logger.info("connector_reload_subscriber.listening", channel=CONNECTOR_RELOAD_CHANNEL, pod=POD_ID)
    loop = asyncio.get_event_loop()
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                payload = json.loads(message.get("data") or "{}")
            except Exception:
                payload = {}
            if payload.get("origin") == POD_ID:
                continue  # our own echo — we already reloaded locally
            logger.info("connector_reload_subscriber.received", origin=payload.get("origin"), pod=POD_ID)
            try:
                # Reload off the event loop so fan-out never blocks request serving.
                result = await loop.run_in_executor(None, _reload_generated_connectors)
                logger.info("connector_reload_subscriber.reloaded", pod=POD_ID, **result)
            except Exception as exc:
                logger.error("connector_reload_subscriber.reload_failed", error=str(exc)[:200], pod=POD_ID)
    except asyncio.CancelledError:
        try:
            await pubsub.unsubscribe(CONNECTOR_RELOAD_CHANNEL)
            await pubsub.aclose()
        except Exception:
            pass
        raise


def _load_installed_connectors() -> int:
    """Register connector ARTIFACTS installed as wheels (from the JFrog PyPI repo).

    This is the artifact/runtime-import half of the connector model:
    ``core/build_artifact.py`` publishes one wheel per connector to JFrog, each
    declaring a ``shielva.connectors`` entry-point; the image ``pip install``s them;
    here we discover every entry-point, import the class, and register it into
    ``CONNECTOR_CLASSES`` keyed by ``CONNECTOR_TYPE``. Fully modular — no filesystem
    scan, no R2 at runtime.

    Complements (does not replace) ``_load_generated_connectors()``, which still
    serves local-dev / SAD-pushed connectors from the on-disk ``generated_connectors/``
    tree. Both populate the same ``CONNECTOR_CLASSES`` registry.
    """
    from importlib.metadata import entry_points

    try:
        eps = list(entry_points(group="shielva.connectors"))
    except TypeError:  # pragma: no cover — Python <3.10 selection API
        eps = list(entry_points().get("shielva.connectors", []))  # type: ignore[attr-defined]

    loaded = 0
    for ep in eps:
        try:
            cls = ep.load()
        except Exception as exc:  # noqa: BLE001 — one bad wheel must not sink startup
            logger.error("connector artifact failed to import", name=ep.name, error=str(exc)[:200])
            continue
        ctype = getattr(cls, "CONNECTOR_TYPE", None) or ep.name
        CONNECTOR_CLASSES[ctype] = cls
        loaded += 1
    if loaded:
        logger.info("installed connector artifacts loaded", count=loaded)
    return loaded


# ── On-demand connector install (runtime pip-install from JFrog) ──────────────
# The image no longer bakes all 213 connector wheels. Instead a connector's wheel
# is pip-installed from the JFrog PyPI index the first time it's needed (install,
# apis, test) or re-hydrated on boot for already-installed connectors. Keeps the
# image small and only lands the SDK dependency closure for connectors in use.
_CONNECTOR_INSTALL_LOCKS: Dict[str, asyncio.Lock] = {}
_WHEEL_VERSIONS: Dict[str, str] = {}


def _norm_dist(name: str) -> str:
    """PEP 503-style normalize a connector_type. NOTE: does NOT strip a
    `_connector` suffix — some wheels keep it (shielva-connector-google-gmail-connector)
    while others don't (shielva-connector-activecampaign). Resolution against the
    manifest (below) tries both forms."""
    import re
    return re.sub(r"[-_.]+", "-", (name or "").strip().lower()).strip("-")


def _resolve_wheel_suffix(connector_type: str) -> Optional[str]:
    """Map a connector_type to the manifest's wheel dist suffix, trying the type
    as-is and with/without a trailing '-connector' (catalog type 'google_gmail'
    and runtime type 'google_gmail_connector' both resolve to the published
    'google-gmail-connector')."""
    _load_wheel_versions()
    n = _norm_dist(connector_type)
    base = n[: -len("-connector")] if n.endswith("-connector") else n
    for cand in (n, f"{base}-connector", base):
        if cand in _WHEEL_VERSIONS:
            return cand
    return None


def _load_wheel_versions() -> None:
    """Parse connectors-requirements.txt -> {dist_suffix: version} (cached)."""
    if _WHEEL_VERSIONS:
        return
    path = os.path.join(os.path.dirname(__file__), "connectors-requirements.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "==" not in line:
                    continue
                if line.startswith("shielva-connector-"):
                    pkg, ver = line.split("==", 1)
                    _WHEEL_VERSIONS[pkg[len("shielva-connector-"):]] = ver.strip()
    except FileNotFoundError:
        logger.warning("connectors-requirements.txt missing — on-demand install cannot pin versions")


async def _ensure_connector_installed(connector_type: str) -> bool:
    """If the connector class isn't loaded, pip-install its wheel from JFrog at
    runtime then re-scan entry-points. Returns True if the class is available."""
    if _resolve_connector_type(connector_type) in CONNECTOR_CLASSES:
        return True
    suffix = _resolve_wheel_suffix(connector_type)
    if not suffix:
        logger.error("on-demand install: no wheel in manifest for connector", connector_type=connector_type)
        return False
    ver = _WHEEL_VERSIONS.get(suffix)
    pkg = f"shielva-connector-{suffix}" + (f"=={ver}" if ver else "")

    token = os.getenv("JFROG_TOKEN") or os.getenv("JFROG_PASSWORD")
    if not token:
        logger.error("on-demand install: JFROG_TOKEN unset", connector_type=connector_type)
        return False
    user = os.getenv("JFROG_USER", "")
    host = os.getenv("JFROG_INDEX_HOST", "trialrifms4.jfrog.io")
    repo = os.getenv("JFROG_REPO", "shielva592-42")
    import urllib.parse as _u
    cred = f"{_u.quote(user, safe='')}:{_u.quote(token, safe='')}@"
    index_url = f"https://{cred}{host}/artifactory/api/pypi/{repo}/simple"

    lock = _CONNECTOR_INSTALL_LOCKS.setdefault(suffix, asyncio.Lock())
    async with lock:
        # Another request may have installed it while we waited for the lock.
        if _resolve_connector_type(connector_type) in CONNECTOR_CLASSES:
            return True
        logger.info("on-demand installing connector wheel", pkg=pkg)
        # Pass index URLs (which carry the token) via env, NOT argv — keeps the
        # credential out of `ps`, pip's echoed command, and error output.
        env = {**os.environ, "PIP_INDEX_URL": index_url,
               "PIP_EXTRA_INDEX_URL": "https://pypi.org/simple/"}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--no-cache-dir",
            "--disable-pip-version-check", "--user", pkg,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            tail = out.decode(errors="replace")[-400:] if out else ""
            logger.error("on-demand pip install failed", pkg=pkg, rc=proc.returncode, tail=tail)
            return False
        import importlib, site
        # The --user install lands in the user-site dir. If that dir didn't exist at
        # process start, site.py never added it to sys.path, so entry_points() can't
        # see the new dist. Add it explicitly, then re-scan.
        usersite = site.getusersitepackages()
        if usersite and usersite not in sys.path:
            site.addsitedir(usersite)
        importlib.invalidate_caches()
        _load_installed_connectors()
        ok = _resolve_connector_type(connector_type) in CONNECTOR_CLASSES
        if not ok:
            logger.error("on-demand install: wheel installed but class not registered", pkg=pkg)
        return ok


def _load_generated_connectors(generated_root: str = None) -> None:
    """Dynamically discover and register AI-generated connectors from generated_connectors/.

    Scans generated_connectors/{tenant_id}/{service_slug}_connector/connector.py,
    imports the module, finds the BaseConnector subclass, and adds it to CONNECTOR_CLASSES
    keyed by its CONNECTOR_TYPE attribute.

    Called at startup so generated connectors are available immediately.
    """
    import importlib.util

    root = Path(generated_root or os.getenv("GENERATED_CODE_DIR") or _DEFAULT_GENERATED_DIR).resolve()
    if not root.exists():
        logger.info("generated_connectors directory not found — skipping dynamic load", path=str(root))
        return

    loaded = 0
    for tenant_dir in root.iterdir():
        if not tenant_dir.is_dir():
            continue
        for pkg_dir in tenant_dir.iterdir():
            if not pkg_dir.is_dir():
                continue
            connector_file = pkg_dir / "connector.py"
            if not connector_file.exists():
                continue

            # Layer 1 — static AST scan BEFORE executing any of this package's code.
            if CONNECTOR_AST_SCAN != "off":
                scan_findings = _scan_connector_package(pkg_dir)
                if scan_findings:
                    if CONNECTOR_AST_SCAN == "enforce":
                        logger.error(
                            "connector_blocked_by_scan",
                            package=pkg_dir.name, tenant=tenant_dir.name,
                            findings=scan_findings,
                            hint="set CONNECTOR_AST_SCAN=warn to load anyway (not recommended)",
                        )
                        continue
                    logger.warning(
                        "connector_scan_findings (loaded anyway — CONNECTOR_AST_SCAN=warn)",
                        package=pkg_dir.name, tenant=tenant_dir.name, findings=scan_findings,
                    )

            # Track sys.path / sys.modules mutations so we can revert them
            # AFTER this connector loads. Without this, the previous connector's
            # `exceptions.py` (a top-level file the loader can't pre-register as
            # a package) wins the bare `from exceptions import ...` lookup for
            # every connector loaded after it — `DomoAuthError not in Freshservice/exceptions.py`.
            _path_added: list[str] = []
            _bare_modules_set: list[str] = []
            # Snapshot sys.modules BEFORE this connector loads so we can
            # remove the bare-name entries (`exceptions`, `models`, etc.)
            # this connector populated. Otherwise Python caches the first
            # connector's `exceptions` module and the next connector's
            # `from exceptions import X` reuses it without scanning sys.path.
            _modules_before = set(sys.modules.keys())
            _pkg_dir_resolved = str(pkg_dir.resolve())
            try:
                # Add package root and shared path to sys.path if needed
                connectors_root = str(Path(__file__).resolve().parent)
                if connectors_root not in sys.path:
                    sys.path.insert(0, connectors_root)
                    _path_added.append(connectors_root)

                # Also add the connector's own directory so that
                # `from exceptions import ...`, `from models import ...`, and
                # other bare-name imports of top-level files in THIS connector
                # resolve correctly. MUST be removed after load (below) so the
                # next connector's bare imports don't resolve to this one.
                pkg_dir_str = str(pkg_dir.resolve())
                if pkg_dir_str not in sys.path:
                    sys.path.insert(0, pkg_dir_str)
                    _path_added.append(pkg_dir_str)

                mod_name = f"generated_{tenant_dir.name}_{pkg_dir.name}"

                # Pre-register subpackages (client/, helpers/, repository/) under the
                # namespaced module name so that sibling connectors don't clash.
                for subpkg in ("client", "helpers", "repository"):
                    subpkg_dir = pkg_dir / subpkg
                    subpkg_init = subpkg_dir / "__init__.py"
                    if subpkg_init.exists():
                        subpkg_mod_name = f"{mod_name}.{subpkg}"
                        subpkg_spec = importlib.util.spec_from_file_location(
                            subpkg_mod_name,
                            subpkg_init,
                            submodule_search_locations=[str(subpkg_dir)],
                        )
                        subpkg_mod = importlib.util.module_from_spec(subpkg_spec)
                        sys.modules[subpkg_mod_name] = subpkg_mod
                        # Also register as bare name so the connector's unqualified
                        # `from helpers.X import Y` resolves. OVERWRITE (not setdefault):
                        # each connector execs synchronously, so the currently-loading
                        # connector's package must win the bare name during its own exec —
                        # otherwise the first connector's `helpers` shadows every sibling
                        # (e.g. gmail's `helpers.gmail_utils` resolving against google's).
                        sys.modules[subpkg] = subpkg_mod
                        _bare_modules_set.append(subpkg)
                        _exec_module_with_timeout(
                            lambda m=subpkg_mod, s=subpkg_spec: s.loader.exec_module(m),
                            CONNECTOR_IMPORT_TIMEOUT_S, f"{mod_name}.{subpkg}",
                        )
                        # Register each .py file inside the subpackage
                        for child_py in subpkg_dir.glob("*.py"):
                            if child_py.name == "__init__.py":
                                continue
                            child_mod_name = f"{mod_name}.{subpkg}.{child_py.stem}"
                            child_spec = importlib.util.spec_from_file_location(child_mod_name, child_py)
                            child_mod = importlib.util.module_from_spec(child_spec)
                            sys.modules[child_mod_name] = child_mod
                            # Overwrite the bare submodule name too (see note above).
                            sys.modules[f"{subpkg}.{child_py.stem}"] = child_mod
                            _bare_modules_set.append(f"{subpkg}.{child_py.stem}")
                            _exec_module_with_timeout(
                                lambda m=child_mod, s=child_spec: s.loader.exec_module(m),
                                CONNECTOR_IMPORT_TIMEOUT_S, child_mod_name,
                            )

                spec = importlib.util.spec_from_file_location(mod_name, connector_file)
                mod = importlib.util.module_from_spec(spec)
                # Register before exec so module-level constants (AUTH_URI, TOKEN_URI)
                # are accessible via sys.modules when base_connector resolves them.
                sys.modules[mod_name] = mod
                # Layer 2 — bound import wall-clock so a hang-on-import can't stall
                # startup / reload for every tenant.
                _exec_module_with_timeout(
                    lambda m=mod, s=spec: s.loader.exec_module(m),
                    CONNECTOR_IMPORT_TIMEOUT_S, mod_name,
                )

                # Find BaseConnector subclass
                from shared.base_connector import BaseConnector as _BaseConnector
                for attr_name in dir(mod):
                    cls = getattr(mod, attr_name)
                    if (
                        isinstance(cls, type)
                        and issubclass(cls, _BaseConnector)
                        and cls is not _BaseConnector
                        and hasattr(cls, "CONNECTOR_TYPE")
                    ):
                        connector_type = cls.CONNECTOR_TYPE
                        if connector_type not in CONNECTOR_CLASSES:
                            CONNECTOR_CLASSES[connector_type] = cls
                            logger.info(
                                "Loaded generated connector",
                                connector_type=connector_type,
                                path=str(connector_file),
                            )
                            loaded += 1
                        break
            except Exception as e:
                import traceback
                logger.warning(
                    "Failed to load generated connector",
                    path=str(connector_file),
                    error=str(e),
                    traceback=traceback.format_exc(),
                )
            finally:
                # Roll back sys.path / sys.modules pollution so the next
                # connector's bare-name imports (`from exceptions import ...`,
                # `from models import ...`) resolve to ITS OWN files, not
                # ones left behind by the connector we just loaded.
                for _p in _path_added:
                    try: sys.path.remove(_p)
                    except ValueError: pass
                for _bm in _bare_modules_set:
                    sys.modules.pop(_bm, None)
                # Drop any newly-cached sys.modules entries whose file lives
                # inside THIS connector's pkg_dir. The next connector imports
                # `exceptions`, `models`, etc. against ITS OWN files — but
                # Python will reuse the cached module if its key still exists.
                for _k in list(sys.modules.keys()):
                    if _k in _modules_before:
                        continue
                    _m = sys.modules.get(_k)
                    _f = getattr(_m, "__file__", None) or ""
                    if _f.startswith(_pkg_dir_resolved):
                        # Keep the fully-qualified namespaced modules
                        # (generated_<tenant>_<pkg>.*) — those won't collide
                        # because they include the connector name. Only drop
                        # the bare-name entries.
                        if not _k.startswith(f"generated_{tenant_dir.name}_"):
                            sys.modules.pop(_k, None)

    logger.info(f"Loaded {loaded} generated connector(s) from {root}")


def _reload_generated_connectors(generated_root: str = None) -> dict:
    """Hot-reload generated connectors: evict stale sys.modules entries, re-scan disk.

    Called after ``git pull`` brings in new/updated connector code. Existing live
    connector *instances* in the registry are NOT destroyed — only the class
    definitions in CONNECTOR_CLASSES are refreshed so the next install/deploy
    picks up the new code.

    Returns summary: { loaded, updated, evicted_modules }
    """
    import importlib.util

    root = Path(generated_root or os.getenv("GENERATED_CODE_DIR") or _DEFAULT_GENERATED_DIR).resolve()
    if not root.exists():
        return {"loaded": 0, "updated": 0, "evicted_modules": 0}

    # 1. Evict all previously-loaded generated_* modules from sys.modules
    #    so importlib re-executes the (possibly updated) source files.
    evicted = 0
    stale_keys = [k for k in sys.modules if k.startswith("generated_")]
    for k in stale_keys:
        del sys.modules[k]
        evicted += 1

    # 2. Track which connector types we already know about
    before = set(CONNECTOR_CLASSES.keys())

    # 3. Allow _load_generated_connectors to overwrite existing entries
    #    by temporarily removing generated connector types from the dict.
    #    (The function skips types that already exist in CONNECTOR_CLASSES.)
    generated_types = []
    for tenant_dir in root.iterdir():
        if not tenant_dir.is_dir():
            continue
        for pkg_dir in tenant_dir.iterdir():
            if not pkg_dir.is_dir():
                continue
            connector_file = pkg_dir / "connector.py"
            if not connector_file.exists():
                continue
            # Read CONNECTOR_TYPE without importing — just parse the file
            try:
                import ast
                tree = ast.parse(connector_file.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id == "CONNECTOR_TYPE":
                                if isinstance(node.value, (ast.Constant, ast.Str)):
                                    ct = node.value.value if isinstance(node.value, ast.Constant) else node.value.s
                                    generated_types.append(ct)
            except Exception:
                pass

    # Remove known generated types so _load_generated_connectors re-imports them
    for ct in generated_types:
        CONNECTOR_CLASSES.pop(ct, None)

    # 4. Re-scan and load
    _load_generated_connectors(str(root))

    after = set(CONNECTOR_CLASSES.keys())
    new_types = after - before
    updated_types = set(generated_types) & before

    logger.info(
        "connector_gateway.hot_reload_complete",
        new=list(new_types),
        updated=list(updated_types),
        evicted_modules=evicted,
    )

    return {
        "loaded": len(new_types),
        "updated": len(updated_types),
        "evicted_modules": evicted,
        "new_types": list(new_types),
        "updated_types": list(updated_types),
    }


class ConnectorRegistry:
    """Registry of active connector instances"""
    
    def __init__(self):
        self._connectors: Dict[str, Any] = {}
    
    def register(self, connector_id: str, connector):
        """Register a connector instance"""
        self._connectors[connector_id] = connector
    
    def get(self, connector_id: str):
        """Get connector by ID"""
        return self._connectors.get(connector_id)

    def find_by_type(self, tenant_id: str, connector_type: str):
        """Find this tenant's deployed instance whose CONNECTOR_TYPE matches.

        Action schemas (and live bot actions) reference a connector by its TYPE
        slug (e.g. "google_gmail_connector"), not by the deployed-instance id the
        registry is keyed on. Resolve the type to the tenant's live instance.
        """
        for inst in self._connectors.values():
            if getattr(inst, "tenant_id", None) == tenant_id and \
               str(getattr(inst, "CONNECTOR_TYPE", "")) == connector_type:
                return inst
        return None
    
    def remove(self, connector_id: str):
        """Remove connector"""
        if connector_id in self._connectors:
            del self._connectors[connector_id]
    
    def list_all(self) -> List[str]:
        """List all connector IDs"""
        return list(self._connectors.keys())


# Global registry
registry = ConnectorRegistry()


# ===== Application Setup =====

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan"""
    import os
    import asyncio
    from shielva_common.discovery_client import DiscoveryClient
    
    # Startup: Discovery Registration
    gateway_url = os.getenv("GATEWAY_URL", "https://localhost:8000")
    api_port = int(os.getenv("CONNECTOR_PORT", 8003))
    
    app.state.discovery = DiscoveryClient(
        service_name="connectors",
        service_port=api_port,
        gateway_url=gateway_url
    )
    asyncio.create_task(app.state.discovery.start())
    logger.info("Connector Gateway registered with Discovery", gateway=gateway_url)

    # Startup: Start Scheduler
    scheduler.start()

    # Register pip-installed connector artifacts (wheels from JFrog), then the
    # on-disk generated tree (local-dev / SAD). Both feed CONNECTOR_CLASSES.
    _load_installed_connectors()
    # Load AI-generated connectors dynamically
    _load_generated_connectors()
    # NOTE: the advanced-connector catalog seed lives in `integration.main`'s
    # lifespan (the actually-deployed entrypoint), not here. gateway.py runs
    # locally / in SAD, never as the prod pod, so seeding from here would never
    # touch prod Mongo.

    # HA: subscribe to the cluster-wide connector-reload channel so a deploy/reload
    # on any pod fans out to this one (no stale/404 connectors across replicas).
    app.state.reload_sub = asyncio.create_task(_connector_reload_subscriber())

    # Startup: Restore Connectors
    stored_connectors = await connector_store.list_connectors()
    for config in stored_connectors:
        try:
            # On-demand: re-hydrate the wheel for this already-installed connector
            # (image no longer bakes them; site-packages is ephemeral per pod).
            await _ensure_connector_installed(config.connector_type)
            if config.connector_type not in CONNECTOR_CLASSES:
                logger.warning(f"Skipping unknown connector type: {config.connector_type}")
                continue
                
            ConnectorClass = CONNECTOR_CLASSES[config.connector_type]
            connector = ConnectorClass(
                tenant_id=config.tenant_id,
                connector_id=config.connector_id,
                config=config.config
            )
            # Re-install (which sets up OAauth handler etc)
            # We assume stored config has everything needed (including secrets from install time)
            # Or waits, does install() need credentials? Yes.
            # If config has them, good.
            await connector.install()
            
            # Initialize (load tokens from Redis)
            await connector.initialize()

            registry.register(config.connector_id, connector)

            # Run a live health check so the registry status reflects reality
            # (otherwise the connector lingers as "offline" in GET /connectors until
            # something probes it). Best-effort — never let a slow/failing probe
            # abort the restore of the remaining connectors.
            try:
                await connector.health_check()
            except Exception as _hc_exc:  # noqa: BLE001
                logger.warning("restore health_check failed", connector_id=config.connector_id, error=str(_hc_exc)[:120])

            logger.info(f"Restored connector: {config.connector_id}")
            
            # Auto-restore schedule if it was active
            if config.schedule_interval and config.kb_id:
                logger.info(f"Auto-restoring schedule for {config.connector_id}", interval=config.schedule_interval, kb_id=config.kb_id)
                scheduler.schedule_connector(
                    connector_id=config.connector_id,
                    kb_id=config.kb_id,
                    interval_seconds=config.schedule_interval
                )
        except Exception as e:
            logger.error(f"Failed to restore connector {config.connector_id}", error=str(e))
            
    logger.info(f"Restored {len(registry.list_all())} connectors from disk")

    yield
    
    # Shutdown: stop the reload subscriber
    if hasattr(app.state, "reload_sub"):
        app.state.reload_sub.cancel()
        try:
            await app.state.reload_sub
        except asyncio.CancelledError:
            pass

    # Shutdown: Discovery Cleanup
    if hasattr(app.state, "discovery"):
        await app.state.discovery.stop()
    
    # Shutdown scheduler
    scheduler.shutdown()
    
    # Cleanup all connectors
    for connector_id in registry.list_all():
        connector = registry.get(connector_id)
        if connector and hasattr(connector, 'close'):
            await connector.close()
    
    logger.info("Connector Gateway shutdown complete")


app = FastAPI(
    title="Shielva Connector Gateway",
    version="1.0.0",
    description="Central gateway for managing Shielva connectors",
    lifespan=lifespan
)

# Ship gateway logs/metrics/traces to shielva-sop (deploy/install/scan/health/sync
# runtime logs). Uses the SOP_* env already in core/.env (SOP_INGESTION_KEY etc.).
# Best-effort: never let observability setup break gateway startup.
try:
    from shielva_common.sop_sdk import setup_sop
    setup_sop(app, service_name="shielva-connectors-gateway")
except Exception as _sop_exc:  # pragma: no cover
    logger.warning("sop_setup_skipped", error=str(_sop_exc))

app.add_middleware(
    CORSMiddleware,
    allow_origins=json.loads(os.getenv("CORS_ORIGINS", '["https://localhost:3010","https://localhost:3001","http://localhost:3010","http://localhost:3000","https://localhost:3000","https://localhost:3005","https://127.0.0.1:3010","http://127.0.0.1:3000"]')),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== Dependencies =====

def get_tenant_id(request: Request) -> str:
    """Extract tenant ID from headers"""
    tenant_id = request.headers.get("X-Tenant-ID")
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Missing X-Tenant-ID")
    return tenant_id


# ===== Endpoints =====

@app.get("/health")
async def health():
    """Health check"""
    return {"status": "healthy", "service": "connector-gateway"}


@app.post("/internal/reload-connectors")
async def reload_connectors():
    """Hot-reload generated connectors from disk.

    Evicts stale module caches and re-imports all generated connector packages.
    Internal endpoint — not exposed through the API gateway.
    """
    try:
        result = _reload_generated_connectors()
        # HA: fan out to every other gateway pod so the whole cluster converges.
        await _publish_connector_reload()
        return {"status": "reloaded", **result}
    except Exception as e:
        logger.error("reload_connectors_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Reload failed: {str(e)[:200]}")


class PullAndReloadRequest(BaseModel):
    """Request body for pull-and-reload."""
    target_branch: str = "connector-development"
    github_token: str            # PAT for authenticated HTTPS pull
    github_repo_url: str         # SSH or HTTPS repo URL


def _parse_repo_for_pull(repo_url: str) -> tuple[str, str]:
    """Extract owner/repo from a GitHub URL (SSH or HTTPS)."""
    url = repo_url.strip()
    if url.startswith("git@"):
        path = url.split(":")[-1].rstrip(".git")
        parts = path.split("/")
        if len(parts) >= 2:
            return parts[0], parts[1]
    url = url.rstrip("/").rstrip(".git")
    parts = url.split("/")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    raise ValueError(f"Invalid GitHub repo URL: {repo_url}")


@app.post("/internal/pull-and-reload")
async def pull_and_reload(
    body: PullAndReloadRequest,
    request: Request,
):
    """Pull latest code from GitHub using the tenant's PAT, then hot-reload connectors.

    This is the single backend API for syncing code after a merge:
      1. Authenticates via PAT (passed in body, encrypted at rest in sync settings)
      2. Runs ``git pull --ff-only`` using HTTPS + PAT (no SSH keys needed)
      3. Hot-reloads all generated connectors from disk

    Called by:
      - The webhook handler after a merge is detected
      - Manually by a super_admin/tenant_admin via the frontend

    Auth: Caller must provide X-User-Role header. Only super_admin and tenant_admin
    can trigger a pull. The webhook handler passes these headers from the sync request doc.
    """
    import asyncio as _asyncio
    import subprocess as _sp
    import hmac as _hmac

    # Authorize EITHER as an internal service (valid X-Internal-Token) OR as an
    # admin user (X-User-Role). Trusted backends (e.g. promote) present the shared
    # token instead of spoofing a role.
    _internal = os.getenv("CONNECTOR_INTERNAL_TOKEN") or ""
    _provided = request.headers.get("X-Internal-Token") or ""
    is_internal = bool(_internal) and _hmac.compare_digest(_provided, _internal)
    user_role = (request.headers.get("X-User-Role") or "viewer").lower()
    if not is_internal and user_role not in ("super_admin", "tenant_admin", "admin"):
        raise HTTPException(
            status_code=403,
            detail=f"Role '{user_role}' cannot trigger pull-and-reload. Requires super_admin/tenant_admin or a valid internal token.",
        )

    result = {"git_pull": None, "reload": None}
    repo_root = Path(__file__).resolve().parent  # shielva-connectors/

    # ── Step 1: fetch + checkout the target branch (PAT-authenticated) ───────
    # The gateway tracks the connector merge branch (e.g. connector-development).
    # FETCH that branch, then CHECKOUT it at the fetched tip — instead of
    # `pull --ff-only`, which fails the moment the local branch and the remote
    # target diverge, leaving merged connector code stranded. `checkout -B`
    # makes the local branch deterministically match the freshly-fetched remote.
    try:
        owner, repo = _parse_repo_for_pull(body.github_repo_url)
        # Build authenticated HTTPS remote: https://<PAT>@github.com/owner/repo.git
        auth_remote = f"https://{body.github_token}@github.com/{owner}/{repo}.git"

        def _run_git(*args: str):
            return _sp.run(
                ["git", *args],
                cwd=str(repo_root), capture_output=True, text=True, timeout=30,
            )

        fetch_proc = await _asyncio.to_thread(_run_git, "fetch", auth_remote, body.target_branch)
        fetch_out = (fetch_proc.stdout + fetch_proc.stderr).strip().replace(body.github_token, "***")
        if fetch_proc.returncode != 0:
            result["git_pull"] = f"fetch failed: {fetch_out[:200]}"
            logger.error("pull_and_reload.git_fetch_failed", branch=body.target_branch, output=fetch_out[:500])
        else:
            co_proc = await _asyncio.to_thread(_run_git, "checkout", "-B", body.target_branch, "FETCH_HEAD")
            co_out = (co_proc.stdout + co_proc.stderr).strip().replace(body.github_token, "***")
            if co_proc.returncode == 0:
                result["git_pull"] = "success"
                logger.info("pull_and_reload.git_checkout_success", branch=body.target_branch, output=co_out[:200])
            else:
                result["git_pull"] = f"checkout failed: {co_out[:200]}"
                logger.error("pull_and_reload.git_checkout_failed", branch=body.target_branch, output=co_out[:500])
    except ValueError as e:
        result["git_pull"] = f"invalid repo URL: {str(e)[:100]}"
    except Exception as e:
        result["git_pull"] = f"error: {str(e)[:100]}"
        logger.error("pull_and_reload.git_error", error=str(e)[:200])

    # ── Step 2: Hot-reload connectors from disk ─────────────────────────────
    try:
        reload_result = _reload_generated_connectors()
        result["reload"] = reload_result
        logger.info("pull_and_reload.reload_success", loaded=reload_result.get("loaded"), updated=reload_result.get("updated"))
        # HA: every pod shares the generated_connectors volume; tell the others to
        # reload now that this pod has pulled the new code.
        await _publish_connector_reload()
    except Exception as e:
        result["reload"] = f"error: {str(e)[:100]}"
        logger.error("pull_and_reload.reload_error", error=str(e)[:200])

    return {"status": "completed", **result}


@app.get("/connectors/list")
async def list_tenant_connectors(
    connector_type: str = None,
    tenant_id: str = Depends(get_tenant_id),
):
    """
    List all deployed connector instances for a tenant.
    Used by the payment service to discover which payment connectors are available.

    Query params:
      connector_type (optional): filter by connector type (e.g. "stripe", "razorpay")

    Returns a list of connector descriptors:
    [
      { "connector_id": "...", "connector_type": "stripe", "status": "connected", ... }
    ]
    """
    from services.connector_store import connector_store

    all_connectors = await connector_store.list_connectors()
    # Filter to this tenant only
    tenant_connectors = [c for c in all_connectors if c.tenant_id == tenant_id]

    if connector_type:
        tenant_connectors = [c for c in tenant_connectors if c.connector_type == connector_type]

    result = []
    for c in tenant_connectors:
        # Check if this connector is in the live registry (meaning it's connected/healthy)
        live = c.connector_id in registry._connectors if hasattr(registry, "_connectors") else False
        status = "connected" if live else "configured"

        result.append({
            "connector_id": c.connector_id,
            "connector_type": c.connector_type,
            "tenant_id": c.tenant_id,
            "status": status,
            "kb_id": c.kb_id,
        })

    return result


@app.get("/connectors/types")
async def list_connector_types():
    """List available connector types — rich metadata from the seeded catalog.

    Source-of-truth order:
      1. Mongo `advanced_connector_catalog` — seeded from the baked snapshot at
         boot via `services.connector_catalog.seed_catalog_if_needed`. Each doc
         is the raw `metadata/connector.json` (display_name, description,
         auth_type, oauth_scopes, apis, install_fields, categories, …).
      2. Live entry-point names from CONNECTOR_CLASSES as fallback (e.g. for
         freshly registered classes the snapshot hasn't caught up to, or local
         dev without a snapshot).
    Plus the legacy coming-soon placeholders.
    """
    try:
        from services.connector_catalog import list_catalog
        catalog = await list_catalog()
    except Exception:  # noqa: BLE001
        catalog = []

    seen: set[str] = set()
    generated: list[dict] = []
    for c in catalog:
        ctype = c.get("connector_type") or c.get("type")
        if not ctype or ctype in seen:
            continue
        seen.add(ctype)
        generated.append({
            "type": ctype,
            "name": c.get("display_name") or c.get("name") or ctype,
            "display_name": c.get("display_name") or c.get("name") or ctype,
            "description": c.get("description") or "",
            "auth_type": c.get("auth_type") or "oauth2",
            "category": (c.get("categories") or [None])[0] if c.get("categories") else c.get("category"),
            "provider": c.get("provider"),
            "service": c.get("service"),
            "version": c.get("version"),
            "oauth_scopes": c.get("oauth_scopes"),
            "install_fields": c.get("install_fields"),
            "features": c.get("features"),
        })
    # Fallback: any class loaded in this pod but missing from the seeded catalog.
    for ct in sorted(CONNECTOR_CLASSES.keys()):
        if ct in seen:
            continue
        seen.add(ct)
        generated.append({"type": ct, "name": ct, "description": "Integration Builder connector", "auth_type": "oauth2"})
    return {
        "connector_types": generated + [
            {
                "type": "teams",
                "name": "Microsoft Teams",
                "description": "Connect to Teams channels and messages",
                "auth_type": "oauth2",
                "coming_soon": True
            },
            {
                "type": "dropbox",
                "name": "Dropbox",
                "description": "Connect to Dropbox files",
                "auth_type": "oauth2",
                "coming_soon": True
            },
            {
                "type": "intercom",
                "name": "Intercom",
                "description": "Connect to Intercom articles and conversations",
                "auth_type": "oauth2",
                "coming_soon": True
            }
        ]
    }


@app.post("/credentials/rotate-key")
async def rotate_credential_key(tenant_id: str = Depends(get_tenant_id)):
    """Rotate this tenant's Data-Encryption-Key (DEK).

    Declared BEFORE the parameterized POST so "rotate-key" isn't captured as a
    connector_type. New credential writes use the new DEK version; existing
    ciphertext keeps decrypting under its retained version. KEK is untouched.
    """
    from services import encryption_service
    try:
        new_version = await encryption_service.rotate_tenant(tenant_id)
    except Exception as e:
        logger.error("Failed to rotate DEK", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "rotated", "active_version": new_version}


@app.post("/credentials/{connector_type}")
async def store_credentials(
    connector_type: str,
    request: CredentialRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Store encrypted credentials for a tenant and connector type.
    """
    try:
        cred_id = await credential_manager.store_credentials(
            tenant_id=tenant_id,
            connector_type=connector_type,
            credentials=request.credentials
        )
        
        return {
            "status": "stored",
            "credential_id": cred_id,
            "connector_type": connector_type
        }
    except Exception as e:
        logger.error("Failed to store credentials", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/credentials/{connector_type}")
async def check_credentials(
    connector_type: str,
    tenant_id: str = Depends(get_tenant_id)
):
    """Check if credentials exist for this connector type."""
    _load_generated_connectors()
    connector_type = _resolve_connector_type(connector_type)
    creds = await credential_manager.get_credentials(tenant_id, connector_type)
    return {
        "exists": bool(creds),
        "connector_type": connector_type
    }


@app.get("/credentials/{connector_type}/values")
async def get_credential_values(
    connector_type: str,
    tenant_id: str = Depends(get_tenant_id)
):
    """Return decrypted credential values for form pre-fill.
    Sensitive fields (client_secret, api_key, password, token) are returned
    as the real value so the form can be fully pre-populated.
    The frontend renders them as password-type inputs.
    """
    _load_generated_connectors()
    connector_type = _resolve_connector_type(connector_type)
    creds = await credential_manager.get_credentials(tenant_id, connector_type)
    if not creds:
        return {"exists": False, "values": {}}
    # Strip internal fields before returning to frontend
    public_values = {k: v for k, v in creds.items() if not k.startswith("_")}
    return {"exists": True, "values": public_values}


@app.post("/connectors/{connector_type}/install", response_model=ConnectorInstallResponse)
async def install_connector(
    connector_type: str,
    request: ConnectorInstallRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Install a new connector.
    
    If credentials are stored in CredentialManager, they will be used.
    Otherwise, they must be provided in request.config.
    """
    logger.info(
        "Installing connector",
        connector_type=connector_type,
        tenant_id=tenant_id
    )

    # Re-scan generated connectors in case a new one was just built
    _load_generated_connectors()
    # On-demand: pull the connector's wheel from JFrog if it isn't loaded yet.
    await _ensure_connector_installed(connector_type)
    connector_type = _resolve_connector_type(connector_type)

    if connector_type not in CONNECTOR_CLASSES:
        raise HTTPException(
            status_code=400,
            detail=f"Connector type '{connector_type}' not found. Ensure the connector has been built successfully.",
        )
    
    # 1. Fetch stored credentials
    stored_creds = await credential_manager.get_credentials(tenant_id, connector_type)
    
    # 2. Merge credentials into config
    # stored_creds take precedence over request.config if both exist for security?
    # Or request.config overrides for one-off?
    # Let's say stored_creds merge into config.
    
    final_config = request.config.copy()
    if stored_creds:
        # Avoid creating a new dict if possible to keep secrets secure in memory only briefly
        # But we need to merge.
        for k, v in stored_creds.items():
            if k not in final_config:
                final_config[k] = v
        logger.info("Injected stored credentials", connector_type=connector_type)
    
    # Generate connector ID
    import uuid
    import hashlib as _hl, time as _time
    _ts = str(int(_time.time() * 1000))
    _hash = _hl.sha256(f"{connector_type}:{tenant_id}:{_ts}".encode()).hexdigest()[:8]
    connector_id = f"{connector_type}_{_hash}"
    
    # Create connector instance
    ConnectorClass = CONNECTOR_CLASSES[connector_type]
    try:
        connector = ConnectorClass(
            tenant_id=tenant_id,
            connector_id=connector_id,
            config=final_config
        )
    except Exception as e:
        logger.error("Failed to instantiate connector", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to initialize connector: {str(e)}")
    
    # Install connector
    status = await connector.install()
    
    # Register connector
    registry.register(connector_id, connector)
    
    # Persist Connector Configuration (including merged credentials)
    # Persist Connector Configuration (including merged credentials)
    await connector_store.save_connector(
        connector_id=connector_id,
        connector_type=connector_type,
        tenant_id=tenant_id,
        config=final_config
    )
    
    # Also ensure we persist credentials to CredentialManager if they were passed in request
    # This ensures they are available for future re-installs or other connectors
    # We check if 'client_secret' or similar sensitive keys are in request.config
    # A generic way is to check if we used stored_creds. If NOT, and we have secrets, store them.
    if not stored_creds:
        # Simple heuristic: store everything if it looks like credentials
        # Or just store specific keys for known types?
        # For now, let's just rely on the user manually calling /credentials endpoint if they want to share creds
        # BUT for resilience, we just relied on connector_store saving the full config (which has secrets).
        # That is sufficient for restoring THIS connector.
        pass
    
    # Get OAuth URL
    oauth_url = None
    if status.auth_status.value == "pending":
        # redirect_uri is the platform's deterministic public OAuth callback. Mirror
        # the /connectors/check default so an OAuth install returns a COMPLETE
        # authorization URL — an empty redirect_uri makes Google reject the consent.
        # Use the PUBLIC gateway origin (the user's browser is redirected there),
        # not the in-cluster GATEWAY_URL used for service discovery.
        _redir_base = os.getenv("PUBLIC_GATEWAY_URL") or os.getenv("GATEWAY_URL", "https://localhost:8000")
        redirect_uri = final_config.get("redirect_uri") or f"{_redir_base}/connectors/oauth/callback"
        # Persist so authorize() reuses the SAME redirect_uri at token exchange.
        connector.config["redirect_uri"] = redirect_uri
        oauth_url = connector.get_oauth_url(redirect_uri, state=connector_id)
    
    return ConnectorInstallResponse(
        connector_id=connector_id,
        connector_type=connector_type,
        status=status.auth_status.value,
        oauth_url=oauth_url
    )


@app.post("/connectors/{connector_type}/test")
async def test_connection(
    connector_type: str,
    request: ConnectorInstallRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Test connector configuration.
    Does not save the connector, just validates config and connectivity.
    """
    if connector_type not in CONNECTOR_CLASSES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown connector type: {connector_type}"
        )
    
    ConnectorClass = CONNECTOR_CLASSES[connector_type]
    
    # Use a temporary ID for testing
    import uuid
    temp_id = f"test_{connector_type}_{uuid.uuid4().hex[:8]}"
    
    try:
        # Initialize connector with provided config
        connector = ConnectorClass(
            tenant_id=tenant_id,
            connector_id=temp_id,
            config=request.config
        )
        
        # Call test_connection method
        result = await connector.test_connection()
        
        return result
        
    except Exception as e:
        logger.error(f"Test connection failed for {connector_type}", error=str(e))
        return {
            "success": False,
            "message": str(e)
        }


@app.post("/connectors/check")
async def check_connector_connection(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
):
    """Validate credentials and return oauth_url for OAuth2 connectors.

    For OAuth2 connectors: calls install() to validate credentials format,
    then returns oauth_url so the frontend can start the authorization flow.

    For API-key connectors: calls health_check() to verify connectivity.

    Does NOT persist anything — pure validation before deploy.

    Body: { connector_type, config, redirect_uri? }
    """
    body = await request.json()
    connector_type = body.get("connector_type", "")
    config = body.get("config", {})
    _gw = os.getenv("PUBLIC_GATEWAY_URL") or os.getenv("GATEWAY_URL", "https://localhost:8000")
    _default_redirect = f"{_gw}/connectors/oauth/callback"
    redirect_uri = body.get("redirect_uri") or config.get("redirect_uri") or _default_redirect

    _load_generated_connectors()
    connector_type = _resolve_connector_type(connector_type)

    if connector_type not in CONNECTOR_CLASSES:
        return {
            "healthy": False,
            "auth_status": "not_found",
            "message": None,
            "error": f"Connector type '{connector_type}' not found. Ensure the connector is built first.",
            "oauth_url": None,
        }

    # ── Backend field validation ──────────────────────────────────────────
    connector_meta = _find_connector_json(connector_type)
    if connector_meta:
        field_errors = _validate_required_fields(config, connector_meta.get("install_fields", []))
        if field_errors:
            return {
                "healthy": False,
                "auth_status": "missing_credentials",
                "message": None,
                "error": "Please fill in all required fields: " + " ".join(field_errors),
                "oauth_url": None,
                "validation_errors": field_errors,
            }

    try:
        ConnectorClass = CONNECTOR_CLASSES[connector_type]
        connector = ConnectorClass(
            tenant_id=tenant_id,
            connector_id=f"check_{connector_type}_{tenant_id}",
            config=config,
        )

        # ── Determine auth flow type ─────────────────────────────────────────
        # Priority: connector class AUTH_TYPE → connector.json auth_type → "oauth2_code"
        auth_type = (
            getattr(ConnectorClass, "AUTH_TYPE", None)
            or (connector_meta.get("auth_type") if connector_meta else None)
            or "oauth2_code"
        ).lower()

        logger.info("check.auth_type_resolved", connector_type=connector_type, auth_type=auth_type)

        # ── 1. No-auth / API Key / Bearer / Basic / HMAC ────────────────────────
        if auth_type in ("none", "no_auth", "api_key", "bearer", "basic", "hmac", "aws_signature", "aws_sigv4"):
            # No token exchange needed — validate directly via install() + health_check()
            # Pass config explicitly so generated connectors that check the parameter (not self.config) work correctly.
            status = await connector.install(config)
            if status.auth_status.value in ("missing_credentials", "invalid_credentials"):
                return {"healthy": False, "auth_status": status.auth_status.value, "message": None,
                        "error": status.error or "Missing or invalid credentials.", "oauth_url": None}
            health = await connector.health_check()
            healthy = health.health.value == "healthy"
            if healthy:
                try:
                    config_to_save = {k: v for k, v in config.items() if v is not None and v != ""}
                    if config_to_save:
                        await credential_manager.store_credentials(tenant_id, connector_type, config_to_save)
                except Exception as _e:
                    logger.warning("check.creds_save_failed", error=str(_e))
            return {"healthy": healthy, "auth_status": health.auth_status.value,
                    "message": health.message if healthy else None,
                    "error": health.error if not healthy else None, "oauth_url": None}

        # ── 2. OAuth2 Client Credentials (machine-to-machine, no popup) ──────────
        elif auth_type == "oauth2_client_credentials":
            status = await connector.install()
            if status.auth_status.value in ("missing_credentials", "invalid_credentials"):
                return {"healthy": False, "auth_status": status.auth_status.value, "message": None,
                        "error": status.error or "Missing or invalid credentials.", "oauth_url": None}
            try:
                await connector.authorize_client_credentials()
            except Exception as _cc_err:
                return {"healthy": False, "auth_status": "invalid_credentials", "message": None,
                        "error": f"Client credentials token exchange failed: {_cc_err}", "oauth_url": None}
            health = await connector.health_check()
            healthy = health.health.value == "healthy"
            if healthy:
                try:
                    config_to_save = {k: v for k, v in config.items() if v is not None and v != ""}
                    if config_to_save:
                        await credential_manager.store_credentials(tenant_id, connector_type, config_to_save)
                except Exception as _e:
                    logger.warning("check.creds_save_failed", error=str(_e))
            return {"healthy": healthy, "auth_status": health.auth_status.value,
                    "message": health.message if healthy else None,
                    "error": health.error if not healthy else None, "oauth_url": None,
                    "test_result": {"healthy": healthy, "auth_status": health.auth_status.value,
                                    "message": health.message if healthy else None,
                                    "error": health.error if not healthy else None}}

        # ── 3. OAuth2 Password Grant (deprecated — exchange username+password for token) ──
        elif auth_type == "oauth2_password":
            status = await connector.install()
            if status.auth_status.value in ("missing_credentials", "invalid_credentials"):
                return {"healthy": False, "auth_status": status.auth_status.value, "message": None,
                        "error": status.error or "Missing or invalid credentials.", "oauth_url": None}
            try:
                await connector.authorize_password_grant()
            except Exception as _pw_err:
                return {"healthy": False, "auth_status": "invalid_credentials", "message": None,
                        "error": f"Password grant failed: {_pw_err}", "oauth_url": None}
            health = await connector.health_check()
            healthy = health.health.value == "healthy"
            if healthy:
                try:
                    config_to_save = {k: v for k, v in config.items() if v is not None and v != ""}
                    if config_to_save:
                        await credential_manager.store_credentials(tenant_id, connector_type, config_to_save)
                except Exception as _e:
                    logger.warning("check.creds_save_failed", error=str(_e))
            return {"healthy": healthy, "auth_status": health.auth_status.value,
                    "message": health.message if healthy else None,
                    "error": health.error if not healthy else None, "oauth_url": None,
                    "test_result": {"healthy": healthy, "auth_status": health.auth_status.value,
                                    "message": health.message if healthy else None,
                                    "error": health.error if not healthy else None}}

        # ── 4. Service Account (Google SA JSON / JWT assertion) ───────────────────
        elif auth_type in ("service_account", "jwt"):
            status = await connector.install()
            if status.auth_status.value in ("missing_credentials", "invalid_credentials"):
                return {"healthy": False, "auth_status": status.auth_status.value, "message": None,
                        "error": status.error or "Missing or invalid credentials.", "oauth_url": None}
            try:
                if auth_type == "service_account":
                    await connector.authorize_service_account()
                else:
                    key_info = config.get("service_account_json") or {}
                    if isinstance(key_info, str):
                        import json as _json
                        key_info = _json.loads(key_info)
                    scopes = config.get("scopes") or getattr(ConnectorClass, "REQUIRED_SCOPES", [])
                    if isinstance(scopes, str):
                        scopes = scopes.split()
                    await connector._authorize_jwt_assertion(key_info, scopes)
            except Exception as _sa_err:
                return {"healthy": False, "auth_status": "invalid_credentials", "message": None,
                        "error": f"Service account / JWT auth failed: {_sa_err}", "oauth_url": None}
            health = await connector.health_check()
            healthy = health.health.value == "healthy"
            if healthy:
                try:
                    config_to_save = {k: v for k, v in config.items() if v is not None and v != ""}
                    if config_to_save:
                        await credential_manager.store_credentials(tenant_id, connector_type, config_to_save)
                except Exception as _e:
                    logger.warning("check.creds_save_failed", error=str(_e))
            return {"healthy": healthy, "auth_status": health.auth_status.value,
                    "message": health.message if healthy else None,
                    "error": health.error if not healthy else None, "oauth_url": None,
                    "test_result": {"healthy": healthy, "auth_status": health.auth_status.value,
                                    "message": health.message if healthy else None,
                                    "error": health.error if not healthy else None}}

        # ── 5. OAuth2 Device Authorization Grant (CLI/TV/headless) ───────────────
        elif auth_type == "oauth2_device":
            status = await connector.install()
            if status.auth_status.value in ("missing_credentials", "invalid_credentials"):
                return {"healthy": False, "auth_status": status.auth_status.value, "message": None,
                        "error": status.error or "Missing or invalid credentials.", "oauth_url": None}
            try:
                device_info = await connector.initiate_device_flow()
            except Exception as _d_err:
                return {"healthy": False, "auth_status": "failed", "message": None,
                        "error": f"Device flow initiation failed: {_d_err}", "oauth_url": None}
            # Save full config so form fields are restored on return
            try:
                config_to_save = {k: v for k, v in config.items() if v is not None and v != ""}
                if config_to_save:
                    await credential_manager.store_credentials(tenant_id, connector_type, config_to_save)
            except Exception as _e:
                logger.warning("check.creds_save_failed", error=str(_e))
            return {
                "healthy": True,
                "auth_status": "pending",
                "message": f"Open {device_info['verification_url']} and enter code: {device_info['user_code']}",
                "error": None,
                "oauth_url": None,
                "auth_type": "oauth2_device",
                "device_code_info": device_info,   # frontend uses this to show the device code UI
            }

        # ── 6. OAuth2 Authorization Code / PKCE (user consent popup) ─────────────
        elif auth_type in ("oauth2_code", "oauth2_pkce", "oauth2"):
            status = await connector.install()
            if status.auth_status.value in ("missing_credentials", "invalid_credentials"):
                return {"healthy": False, "auth_status": status.auth_status.value, "message": None,
                        "error": status.error or "Missing or invalid credentials.", "oauth_url": None}

            # ── Smart re-auth: skip OAuth when credentials are unchanged ─────────
            # If client_id + client_secret + scopes match what is already stored AND
            # a valid token exists in Redis under the canonical key, skip the OAuth
            # popup entirely and verify via health_check() instead.
            #
            # Match is determined by:
            #   1. Stored _auth_hash equals computed hash  (fast path — set after first auth)
            #   2. Direct field comparison fallback         (handles sessions where _auth_hash
            #                                               was never stored, e.g. pre-existing
            #                                               auth before this feature was added)
            import hashlib as _hashlib, json as _json
            _AUTH_KEYS = ("client_id", "client_secret", "scopes")
            _auth_hash = _hashlib.sha256(
                _json.dumps(
                    {k: str(config.get(k, "")) for k in _AUTH_KEYS} | {"connector_type": connector_type},
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:16]

            stored_creds = await credential_manager.get_credentials(tenant_id, connector_type)

            # Determine whether submitted credentials match what is stored
            _creds_match = False
            if stored_creds:
                if stored_creds.get("_auth_hash") == _auth_hash:
                    # Fast path: hash present and matches
                    _creds_match = True
                else:
                    # Fallback: compare raw field values (handles legacy sessions without hash)
                    _creds_match = all(
                        str(stored_creds.get(k, "")).strip() == str(config.get(k, "")).strip()
                        for k in _AUTH_KEYS
                    )

            if _creds_match:
                # Same credentials — try health check with existing token.
                # We attempt the canonical key first (set on every successful OAuth callback).
                # If no token is found there, we scan the registry for any live connector
                # instance of the same type/tenant that already has a token in memory.
                ConnectorClass = CONNECTOR_CLASSES[connector_type]
                _canonical_id = f"canonical_{connector_type}_{tenant_id}"
                _base_config = {**stored_creds, **config}

                # Build candidate connector IDs to try, in priority order:
                #   1. canonical key (most recent successful auth)
                #   2. any live registry entry for this type + tenant
                _candidate_ids = [_canonical_id]
                for _reg_id, _reg_conn in list(registry._connectors.items()):
                    if (
                        getattr(_reg_conn, "tenant_id", None) == tenant_id
                        and getattr(_reg_conn, "CONNECTOR_TYPE", None) == connector_type
                        and _reg_id not in _candidate_ids
                    ):
                        _candidate_ids.append(_reg_id)

                _reuse_success = False
                for _try_id in _candidate_ids:
                    try:
                        _reuse_connector = ConnectorClass(
                            tenant_id=tenant_id,
                            connector_id=_try_id,
                            config=_base_config,
                        )
                        await _reuse_connector.initialize()
                        if not _reuse_connector._token_info:
                            continue   # no token loaded — try next candidate
                        _health = await _reuse_connector.health_check()
                        if _health.health.value == "healthy":
                            # Token still valid — persist updated config + write canonical key
                            _merged = {**stored_creds, **{k: v for k, v in config.items() if v is not None and v != ""}}
                            _merged["_auth_hash"] = _auth_hash   # ensure hash is always up to date
                            await credential_manager.store_credentials(tenant_id, connector_type, _merged)
                            # Backfill canonical key with a guaranteed-await save
                            if _try_id != _canonical_id:
                                try:
                                    from services.connector_store import connector_store as _cs2
                                    _ti = _reuse_connector._token_info
                                    await _cs2.save_connector_tokens(_canonical_id, {
                                        "access_token":  _ti.access_token,
                                        "token_type":    _ti.token_type or "Bearer",
                                        "refresh_token": _ti.refresh_token,
                                        "expires_at":    _ti.expires_at.isoformat() if _ti.expires_at else None,
                                        "scope":         " ".join(_ti.scopes) if _ti.scopes else None,
                                        "raw":           _ti.raw,
                                    })
                                except Exception:
                                    pass
                            logger.info("check.reused_token", connector_type=connector_type,
                                        tenant_id=tenant_id, via=_try_id)
                            return {
                                "healthy": True, "auth_status": "connected",
                                "message": "Connection verified — using existing token.",
                                "error": None, "oauth_url": None, "auth_type": auth_type,
                                "test_result": {
                                    "healthy": True, "auth_status": "connected",
                                    "message": _health.message, "error": None,
                                },
                            }
                        else:
                            # Token exists but health failed — stop trying (don't just fall through
                            # to OAuth silently; surface the real error)
                            logger.info("check.reuse_health_failed", connector_type=connector_type,
                                        error=_health.error)
                            break
                    except Exception as _try_err:
                        logger.info("check.reuse_candidate_failed", connector_id=_try_id,
                                    reason=str(_try_err))
                        continue   # try next candidate

                # All candidates failed — fall through to OAuth so user can re-authorise

            use_pkce = auth_type == "oauth2_pkce"
            # Persist redirect_uri in connector config so authorize() can use it later
            connector.config["redirect_uri"] = redirect_uri
            try:
                oauth_url = connector.get_oauth_url(redirect_uri, state=f"check_{connector_type}_{tenant_id}", use_pkce=use_pkce)
            except Exception as oauth_err:
                return {"healthy": False, "auth_status": "failed", "message": None,
                        "error": f"Could not build authorization URL: {oauth_err}", "oauth_url": None}

            # ── Pre-flight credential probe ───────────────────────────────────
            # Send a deliberately-invalid code to the TOKEN_URI to get the provider
            # to reveal whether client_id / client_secret / scopes are accepted.
            # "invalid_grant" = credentials valid (expected rejection of fake code).
            # "invalid_client" = wrong client_id or client_secret → return error now.
            # Unknown errors are ignored so we don't block legitimate connectors.
            try:
                probe = await connector.probe_oauth_credentials(redirect_uri)
                if not probe["valid"]:
                    logger.info(
                        "check.probe_failed",
                        connector_type=connector_type,
                        error=probe.get("error"),
                    )
                    return {
                        "healthy": False,
                        "auth_status": "invalid_credentials",
                        "message": None,
                        "error": probe["message"],
                        "oauth_url": None,
                        "auth_type": auth_type,
                    }
                logger.info("check.probe_passed", connector_type=connector_type, note=probe.get("message"))
            except Exception as _probe_err:
                # Never let probe failure block the OAuth flow
                logger.warning("check.probe_exception", connector_type=connector_type, error=str(_probe_err))

            # Do NOT persist yet — credentials are only format-valid at this point.
            # Persistence (with _auth_hash) happens after OAuth in /connectors/{id}/callback.
            return {"healthy": True, "auth_status": "pending",
                    "message": None,
                    "error": None, "oauth_url": oauth_url, "auth_type": auth_type}

        # ── 7. Unknown auth type ──────────────────────────────────────────────────
        else:
            return {"healthy": False, "auth_status": "unsupported", "message": None,
                    "error": f"Unknown auth_type '{auth_type}'. Supported: api_key, bearer, basic, hmac, "
                             "oauth2_code, oauth2_pkce, oauth2_client_credentials, oauth2_password, "
                             "oauth2_device, service_account, jwt, none.",
                    "oauth_url": None}

    except Exception as e:
        logger.exception("check.unexpected_error", connector_type=connector_type)
        return {
            "healthy": False,
            "auth_status": "failed",
            "message": None,
            "error": str(e),
            "oauth_url": None,
        }


@app.post("/connectors/check/device-poll")
async def poll_device_authorization(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
):
    """Poll for device authorization completion.

    Called by frontend repeatedly (every `interval` seconds) after showing
    the device code to the user. Returns status: 'pending' | 'connected' | 'error'.

    Body: { connector_type, device_code }
    """
    body = await request.json()
    connector_type = body.get("connector_type", "")
    device_code = body.get("device_code", "")

    if not connector_type or not device_code:
        raise HTTPException(status_code=400, detail="connector_type and device_code are required")

    _load_generated_connectors()
    connector_type = _resolve_connector_type(connector_type)
    if connector_type not in CONNECTOR_CLASSES:
        raise HTTPException(status_code=404, detail=f"Connector type '{connector_type}' not found")

    stored_creds = await credential_manager.get_credentials(tenant_id, connector_type) or {}
    ConnectorClass = CONNECTOR_CLASSES[connector_type]
    connector = ConnectorClass(
        tenant_id=tenant_id,
        connector_id=f"device_poll_{connector_type}_{tenant_id}",
        config={**stored_creds},
    )

    try:
        await connector.poll_device_token(device_code)
        # Token is now stored — run health_check
        health = await connector.health_check()
        healthy = health.health.value == "healthy"
        # Persist full config after successful device token exchange
        try:
            config_to_save = {k: v for k, v in stored_creds.items() if v is not None and v != ""}
            if config_to_save:
                await credential_manager.store_credentials(tenant_id, connector_type, config_to_save)
        except Exception:
            pass
        return {
            "status": "connected" if healthy else "error",
            "healthy": healthy,
            "message": health.message if healthy else None,
            "error": health.error if not healthy else None,
        }
    except RuntimeError as e:
        error_msg = str(e)
        if "authorization_pending" in error_msg:
            return {"status": "pending", "healthy": False, "message": "Waiting for user to authorize...", "error": None}
        if "slow_down" in error_msg:
            return {"status": "slow_down", "healthy": False, "message": "Polling too fast, slow down.", "error": None}
        return {"status": "error", "healthy": False, "message": None, "error": error_msg}
    except ValueError as e:
        return {"status": "error", "healthy": False, "message": None, "error": str(e)}
    except Exception as e:
        return {"status": "error", "healthy": False, "message": None, "error": str(e)}


@app.post("/connectors/deploy")
async def deploy_connector(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
):
    """Enqueue an install job. Returns immediately with a `job_id`.

    Clients poll `GET /connectors/deploy/jobs/{job_id}` until status is
    `completed` (result carries `connector_id`/`oauth_url`/etc.) or `failed`
    (`error` carries the reason). This is non-blocking so multiple installs
    can be triggered back-to-back without the browser timing out; the worker
    pool serialises the heavy connector-loading work and runs install +
    initialize + health_check off the request thread.
    """
    _ensure_deploy_runtime()
    assert _DEPLOY_QUEUE is not None
    body = await request.json()
    job_id = _uuid_mod.uuid4().hex
    _job(job_id)  # initialise as queued
    await _DEPLOY_QUEUE.put((job_id, body, tenant_id))
    logger.info(
        "deploy.job_queued",
        job_id=job_id,
        session_id=body.get("session_id"),
        connector_type=body.get("connector_type"),
        queue_depth=_DEPLOY_QUEUE.qsize(),
    )
    return {"job_id": job_id, "status": "queued"}


@app.get("/connectors/deploy/jobs/{job_id}")
async def deploy_job_status(job_id: str):
    """Return the current status of a queued/running/completed deploy job.

    Useful for one-shot status checks (e.g. a page reload that didn't keep the
    SSE stream open). For live completion notification prefer the `/events`
    SSE endpoint — it pushes the moment the worker finishes instead of waiting
    for the next poll tick.
    """
    job = _DEPLOY_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Deploy job not found (may have expired)")
    return _serialise_job(job_id, job)


@app.get("/connectors/deploy/jobs/{job_id}/events")
async def deploy_job_events(job_id: str, request: Request):
    """Server-Sent Events stream for a deploy job.

    Emits one `data:` frame with the current state on open, then a single
    terminal frame the moment the worker finishes (push, not poll — backed by
    the per-job `asyncio.Event`). 15 s SSE comment keepalives keep proxies
    from closing the connection on a long-running OAuth installer.
    """
    from fastapi.responses import StreamingResponse
    import json as _json

    job = _DEPLOY_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Deploy job not found (may have expired)")

    async def gen():
        # Initial frame so the client sees current state even if it joined late.
        yield f"data: {_json.dumps(_serialise_job(job_id, job))}\n\n"
        if job.get("status") in ("completed", "failed"):
            return

        evt = job.get("_done_event")
        # Loop with a short wait so SSE keepalives go out and disconnects are detected.
        while True:
            if await request.is_disconnected():
                return
            try:
                if evt is None:
                    # Defensive: job created without an event (older schema).
                    # Fall back to a 1 s sleep+recheck.
                    await asyncio.sleep(1.0)
                    if job.get("status") in ("completed", "failed"):
                        break
                    continue
                await asyncio.wait_for(evt.wait(), timeout=15.0)
                break  # event fired → terminal state
            except asyncio.TimeoutError:
                # 15 s passed without completion — send keepalive, stay connected.
                yield ": keepalive\n\n"

        # Final frame with the terminal state.
        yield f"data: {_json.dumps(_serialise_job(job_id, job))}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering when proxied
            "Connection": "keep-alive",
        },
    )


async def _run_deploy_pipeline(body: dict, tenant_id: str) -> dict:
    """Actual install pipeline. Called by the worker pool, not directly by the HTTP handler.

    Body:
        connector_type: str   — matches CONNECTOR_TYPE in connector.py
        session_id: str       — integration builder session ID (for metadata lookup)
        config: dict          — install-time credentials/settings
    """
    connector_type = body.get("connector_type", "")
    config = body.get("config", {})
    session_id = body.get("session_id", "")

    # Server-side connector_type resolution: if the client didn't supply a
    # connector_type (or supplied a generic service name), derive it from
    # session_id by reading the generated connector.json metadata.
    if (not connector_type or connector_type == session_id) and session_id:
        try:
            import httpx as _httpx
            _integration_url = os.getenv("INTEGRATION_SERVICE_URL", "https://localhost:8055")
            async with _httpx.AsyncClient(verify=False) as _c:
                _r = await _c.get(
                    f"{_integration_url}/sessions/{session_id}/connector-metadata",
                    headers={"X-Tenant-ID": tenant_id},
                    timeout=5.0,
                )
                if _r.status_code == 200:
                    _meta = _r.json()
                    # The metadata response doesn't always carry an explicit
                    # connector_type; fall back to `service` / `connector_name`,
                    # which equal CONNECTOR_TYPE for generated connectors. Without
                    # this the deploy 400s ("connector_type could not be resolved").
                    connector_type = (
                        _meta.get("connector_type")
                        or _meta.get("service")
                        or _meta.get("connector_name")
                        or connector_type
                    )
        except Exception as _ex:
            logger.warning("deploy.connector_type_resolution_failed", session_id=session_id, error=str(_ex))

    if not connector_type:
        raise HTTPException(status_code=400, detail="connector_type is required and could not be resolved from session_id")

    # Re-scan generated connectors in case a new one was just built. Serialised
    # with _LOAD_LOCK so concurrent workers don't trample sys.path / sys.modules
    # mid-load (the loader does heavy in-place module registration and rollback —
    # interleaving two of them produces "module not found" / "wrong class" races
    # that look like the original symptom: 1-2 installs succeed, the rest fail
    # with bogus errors).
    # The lock acquisition is itself bounded with DEPLOY_LOAD_TIMEOUT_S — a
    # single connector that hangs in `exec_module` (e.g. a slow import side-
    # effect) can hold the lock at most that long before the lock-waiter gives
    # up. The waiter then proceeds without the lock; if its own load is fine
    # it succeeds, and the hung one fails on its own (per-pipeline timeout
    # catches the original). Result: ONE bad connector cannot wedge every
    # other install.
    if _LOAD_LOCK is not None:
        try:
            await asyncio.wait_for(_LOAD_LOCK.acquire(), timeout=DEPLOY_LOAD_TIMEOUT_S)
            try:
                _load_generated_connectors()
            finally:
                _LOAD_LOCK.release()
        except asyncio.TimeoutError:
            logger.warning(
                "deploy.load_lock_timeout",
                connector_type=connector_type,
                hint="another install is holding the loader — proceeding without lock; isolated load may race",
            )
            _load_generated_connectors()
    else:
        _load_generated_connectors()
    connector_type = _resolve_connector_type(connector_type)

    if connector_type not in CONNECTOR_CLASSES:
        raise HTTPException(
            status_code=404,
            detail=f"Connector type '{connector_type}' not found. Ensure the connector has been built successfully.",
        )

    # Install creates the connector instance and returns a connector_id.
    # Credentials are NOT required at install time — they are provided and
    # validated during the "Test Connection" step (POST /connectors/check).
    ConnectorClassEarly = CONNECTOR_CLASSES[connector_type]

    # Merge any config passed in the request with already-stored credentials
    # (e.g. on a reinstall where creds were previously saved).
    stored_creds = await credential_manager.get_credentials(tenant_id, connector_type)
    final_config = config.copy()
    if stored_creds:
        for k, v in stored_creds.items():
            if k not in final_config:
                final_config[k] = v

    import hashlib as _hl, time as _time
    _ts = str(int(_time.time() * 1000))
    _hash = _hl.sha256(f"{connector_type}:{tenant_id}:{_ts}".encode()).hexdigest()[:8]
    connector_id = f"{connector_type}_{_hash}"

    try:
        connector = ConnectorClassEarly(
            tenant_id=tenant_id,
            connector_id=connector_id,
            config=final_config,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize connector: {e}")

    status = await connector.install()
    registry.register(connector_id, connector)

    await connector_store.save_connector(
        connector_id=connector_id,
        connector_type=connector_type,
        tenant_id=tenant_id,
        config=final_config,
    )

    # ── Load stored token from Redis and run a real test API call ──────────
    # initialize() loads the OAuth token that was stored after authorization.
    # health_check() is implemented by every connector and makes a live API
    # call — for Gmail this is users().getProfile(), for others their equivalent.
    # This is the "dynamic" test: no hardcoding, each connector decides what
    # to call in its own health_check() implementation.
    test_result: dict | None = None
    oauth_url = None

    try:
        import asyncio as _asyncio
        await _asyncio.wait_for(connector.initialize(), timeout=8.0)
        health = await _asyncio.wait_for(connector.health_check(), timeout=10.0)
        test_healthy = health.health.value == "healthy"
        test_result = {
            "healthy": test_healthy,
            "auth_status": health.auth_status.value,
            "message": health.message if test_healthy else None,
            "error": health.error if not test_healthy else None,
        }
        logger.info(
            "deploy.test_api_call",
            connector_id=connector_id,
            connector_type=connector_type,
            healthy=test_healthy,
        )
    except Exception as _health_err:
        # Token not yet stored (OAuth pending) or health check timed out — not fatal
        logger.warning("deploy.test_api_skipped", connector_id=connector_id, reason=str(_health_err))
        test_result = None

    # If still pending after test (no token yet), generate OAuth URL
    effective_status = status.auth_status.value
    if test_result and test_result["healthy"]:
        effective_status = "connected"
    elif test_result and not test_result["healthy"] and test_result["auth_status"] not in ("pending", "missing_credentials"):
        effective_status = test_result["auth_status"]

    # redirect_uri is a deterministic constant ({GATEWAY_URL}/connectors/oauth/callback)
    # and is needed by authorize() at token-exchange time for ANY OAuth flow. Set it on
    # the deployed instance UNCONDITIONALLY — not gated on status — so a connector that
    # needs to (re)authorize (pending, missing_credentials, token_expired, failed, …)
    # always has it. The old status gate skipped token_expired/failed, which broke
    # re-authorization. Non-OAuth connectors simply ignore the value.
    _gw = os.getenv("PUBLIC_GATEWAY_URL") or os.getenv("GATEWAY_URL", "https://localhost:8000")
    _default_redirect = f"{_gw}/connectors/oauth/callback"
    redirect_uri = final_config.get("redirect_uri") or _default_redirect
    connector.config["redirect_uri"] = redirect_uri

    # Generate the consent URL whenever the connector is not already connected, so any
    # non-connected state can start/restart the OAuth flow.
    if effective_status != "connected":
        try:
            oauth_url = connector.get_oauth_url(redirect_uri, state=connector_id)
        except Exception:
            pass

    return {
        "connector_id": connector_id,
        "connector_type": connector_type,
        "status": effective_status,
        "oauth_url": oauth_url,
        "session_id": session_id,
        "test_result": test_result,  # real API call result — None if OAuth not done yet
    }


@app.post("/connectors/{connector_id}/reauthorize")
async def reauthorize_connector(
    connector_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    """Generate a fresh OAuth authorization URL for an ALREADY-installed connector.

    Re-auth is needed when the access token expired and there's no usable refresh
    token. The connector still holds its stored client_id/secret, so we build the
    auth URL from it (base_connector.get_oauth_url adds access_type=offline +
    prompt=consent for Google, so this run yields a refresh token). The FE opens it
    in the OAuth popup and exchanges the code via /connectors/{id}/callback.
    """
    connector = registry.get(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    _base = os.getenv("PUBLIC_GATEWAY_URL") or os.getenv("GATEWAY_URL", "https://localhost:8000")
    redirect_uri = f"{_base}/connectors/oauth/callback"
    connector.config["redirect_uri"] = redirect_uri
    try:
        oauth_url = connector.get_oauth_url(redirect_uri, state=connector_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("reauthorize get_oauth_url failed", connector_id=connector_id, error=str(exc)[:200])
        raise HTTPException(status_code=400, detail=f"Could not build authorization URL: {str(exc)[:160]}")
    return {"oauth_url": oauth_url, "connector_id": connector_id}


@app.get("/connectors/{connector_type}/docs")
async def get_connector_docs(
    connector_type: str,
    tenant_id: str = Depends(get_tenant_id),
):
    """Serve the connector's authored documentation, bundled in the wheel.

    The docs travel with the connector artifact (build_artifact lifts
    .shielva/docs/connector_docs.json → {pkg}/_shielva_docs.json). We read it
    live from the loaded connector class's package — single source of truth, no
    static snapshot. Returns the {title, sections} doc tree the SiteRenderer expects.
    """
    import json as _json
    from pathlib import Path as _Path
    resolved = _resolve_connector_type(connector_type)
    cls = CONNECTOR_CLASSES.get(resolved)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"Connector type '{connector_type}' not found")
    mod = sys.modules.get(cls.__module__)
    mod_file = getattr(mod, "__file__", None) if mod else None
    if mod_file:
        docs_path = _Path(mod_file).parent / "_shielva_docs.json"
        if docs_path.exists():
            try:
                return _json.loads(docs_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.error("connector docs read failed", connector_type=resolved, error=str(exc)[:200])
                raise HTTPException(status_code=500, detail="Failed to read documentation")
    raise HTTPException(status_code=404, detail="No documentation bundled for this connector")


@app.get("/connectors/{connector_id}/apis")
async def list_connector_apis(
    connector_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    """Return the API catalogue from metadata/connector.json for this connector.

    Falls back to introspecting the connector class if connector.json is missing.
    """
    connector = registry.get(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    connector_type = connector.CONNECTOR_TYPE
    metadata: dict | None = None

    # 1. Wheel-installed connectors bundle metadata/connector.json IN their package
    #    (build_artifact ships metadata/* as package-data). Read it directly from the
    #    loaded class's package dir — the generated_connectors filesystem scan below
    #    only exists for local/SAD connectors and FileNotFound'd here (→ the 500).
    try:
        _mod = sys.modules.get(type(connector).__module__)
        _mf = getattr(_mod, "__file__", None) if _mod else None
        if _mf:
            _pkg_meta = Path(_mf).parent / "metadata" / "connector.json"
            if _pkg_meta.exists():
                metadata = json.loads(_pkg_meta.read_text(encoding="utf-8"))
    except Exception:
        metadata = None

    # 2. Fallback: scan the on-disk generated_connectors tree (guarded — the dir may
    #    not exist in a wheels-only deployment).
    if metadata is None:
        try:
            generated_root = Path(os.getenv("GENERATED_CODE_DIR") or _DEFAULT_GENERATED_DIR).resolve()
            for tenant_dir in generated_root.iterdir():
                if not tenant_dir.is_dir():
                    continue
                for pkg_dir in tenant_dir.iterdir():
                    meta_file = pkg_dir / "metadata" / "connector.json"
                    if meta_file.exists():
                        try:
                            candidate = json.loads(meta_file.read_text())
                            if candidate.get("connector_type") == connector_type and tenant_dir.name == tenant_id:
                                metadata = candidate
                                break
                        except Exception:
                            pass
                if metadata:
                    break
        except (FileNotFoundError, OSError):
            pass

    # Internal/system methods that must never appear in the Test APIs step
    _SYSTEM_METHODS = {
        "install", "authorize", "disconnect", "get_metadata", "get_oauth_url",
        "get_status", "initialize", "close", "ingest_batch", "set_token", "get_token",
        "save_config", "report_status", "test_connection", "health_check", "health check",
    }

    def _is_system_api(api: dict) -> bool:
        # "method" in connector.json is the HTTP verb (POST/GET), NOT the function name.
        # The function name lives in "id". Check id first, then fall back to name.
        identifier = (api.get("id") or api.get("name") or "").lower().replace(" ", "_")
        return identifier in _SYSTEM_METHODS

    if metadata:
        # Normalize params: connector.json uses "name" but the frontend ApiParam interface
        # expects "key". Map name→key and synthesize a "label" if absent so that React
        # list rendering never receives undefined keys.
        # The catalogue is stored under "apis" (rich, with params) OR "methods" (name +
        # description only). Prefer "apis"; fall back to "methods".
        raw_apis = metadata.get("apis") or metadata.get("methods") or []
        normalized_apis = []
        for api in raw_apis:
            if _is_system_api(api):
                continue  # strip internal methods from the user-facing Test APIs list
            normalized_params = []
            for p in api.get("params", []):
                param_key = p.get("key") or p.get("name", "")
                normalized_params.append({
                    **p,
                    "key": param_key,
                    "label": p.get("label") or param_key.replace("_", " ").title(),
                })
            # "methods" entries carry no params — enrich from the connector class
            # signature so the Test APIs form still shows input fields.
            fn_name = (api.get("id") or api.get("name") or "").replace(" ", "_")
            if not normalized_params and fn_name:
                import inspect as _insp
                _fn = getattr(connector.__class__, fn_name, None)
                if callable(_fn):
                    try:
                        for pn, pv in _insp.signature(_fn).parameters.items():
                            if pn in ("self", "cls"):
                                continue
                            normalized_params.append({
                                "key": pn,
                                "label": pn.replace("_", " ").title(),
                                "type": "text",
                                "required": pv.default is _insp.Parameter.empty,
                            })
                    except Exception:
                        pass
            normalized_apis.append({**api, "id": api.get("id") or fn_name, "params": normalized_params})
        return {
            "connector_id": connector_id,
            "connector_type": connector_type,
            "version": metadata.get("version", "1.0.0"),
            "apis": normalized_apis,
            "install_fields": metadata.get("install_fields", []),
        }

    # Fallback: introspect class methods
    import inspect
    skip = _SYSTEM_METHODS
    apis = []
    for name, method in inspect.getmembers(connector.__class__, predicate=inspect.isfunction):
        if name.startswith("_") or name in skip:
            continue
        sig = inspect.signature(method)
        params = [
            {"key": p, "label": p.replace("_", " ").title(), "type": "text", "required": pv.default is inspect.Parameter.empty}
            for p, pv in sig.parameters.items() if p not in ("self", "cls")
        ]
        apis.append({"id": name, "name": name.replace("_", " ").title(), "method": name, "params": params})

    return {"connector_id": connector_id, "connector_type": connector_type, "version": "unknown", "apis": apis}


@app.post("/connectors/{connector_id}/test/{method_name}")
async def test_connector_method(
    connector_id: str,
    method_name: str,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
):
    """Invoke a specific method on a deployed connector for testing.

    Body: JSON dict of parameters matching the method signature.
    Returns the method's return value serialized as JSON.
    """
    import inspect, dataclasses, uuid as _uuid

    connector = registry.get(connector_id)
    if not connector:
        # The action-schema bridge (and live bot actions) reference a connector by
        # TYPE slug (e.g. "google_gmail_connector"), not by the deployed-instance id
        # the registry is keyed on — so a direct registry.get() misses. Resolve the
        # type to the tenant's live instance; if none is deployed, build a temp
        # instance from CONNECTOR_CLASSES (mirrors the /connectors/{type}/test path).
        # Either way the credential hydration below loads the tenant's stored creds
        # by CONNECTOR_TYPE, so the method runs exactly as a live action would.
        _ctype = _resolve_connector_type(connector_id)
        if _ctype not in CONNECTOR_CLASSES:
            _load_generated_connectors()  # pick up a freshly-built generated connector
            _ctype = _resolve_connector_type(connector_id)
        connector = registry.find_by_type(tenant_id, _ctype)
        if connector is None:
            _cls = CONNECTOR_CLASSES.get(_ctype)
            if _cls is None:
                raise HTTPException(status_code=404, detail="Connector not found")
            connector = _cls(
                tenant_id=tenant_id,
                connector_id=f"test_{_ctype}_{_uuid.uuid4().hex[:8]}",
                config={},
            )
    if connector.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Allowlist: only public methods
    if method_name.startswith("_"):
        raise HTTPException(status_code=400, detail="Private methods cannot be invoked")

    method = getattr(connector, method_name, None)
    if method is None or not callable(method):
        raise HTTPException(status_code=404, detail=f"Method '{method_name}' not found")

    params = {}
    try:
        params = await request.json()
    except Exception:
        params = {}

    # Hydrate connector with stored credentials before invoking any method.
    # The connector may have been installed with empty config (credentials-free install),
    # with credentials only saved later during Check Connection. Reload from Redis now.
    try:
        _stored = await credential_manager.get_credentials(tenant_id, connector.CONNECTOR_TYPE)
        if _stored:
            await connector.install(_stored)
    except Exception as _hydrate_err:
        logger.warning("test_method.hydrate_failed", connector_id=connector_id, error=str(_hydrate_err))

    # Silent token refresh (universal, all connectors): for any OAuth connector, refresh
    # the short-lived access token from the stored long-lived refresh token BEFORE
    # invoking the method, and persist it (ensure_token → on_token_refresh → set_token).
    # The customer never re-authorizes while the refresh token is valid; re-auth is only
    # needed if the refresh token itself is revoked/expired (provider publishing policy).
    try:
        if hasattr(connector, "ensure_token") and str(getattr(connector, "AUTH_TYPE", "")).lower().startswith("oauth"):
            await connector.ensure_token()
    except Exception as _tok_err:
        logger.info("test_method.token_refresh_skipped", connector_id=connector_id, error=str(_tok_err)[:160])

    try:
        # Layer 3 — bound the invocation wall-clock, and offload SYNC connector
        # methods to a worker thread so a runaway/blocking method can't freeze the
        # gateway event loop for every other tenant.
        if inspect.iscoroutinefunction(method):
            result = await asyncio.wait_for(method(**params), timeout=CONNECTOR_INVOKE_TIMEOUT_S)
        else:
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: method(**params)),
                timeout=CONNECTOR_INVOKE_TIMEOUT_S,
            )

        # Serialize dataclasses / Pydantic models to dict
        if dataclasses.is_dataclass(result):
            result = dataclasses.asdict(result)
        elif hasattr(result, "model_dump"):
            result = result.model_dump()
        elif hasattr(result, "dict"):
            result = result.dict()

        return {"status": "ok", "method": method_name, "result": result}

    except asyncio.TimeoutError:
        logger.error(
            "test_connector_method timeout",
            connector_id=connector_id, method=method_name, timeout=CONNECTOR_INVOKE_TIMEOUT_S,
        )
        return {
            "status": "error", "method": method_name,
            "error": f"Connector method timed out after {CONNECTOR_INVOKE_TIMEOUT_S}s",
        }
    except Exception as e:
        logger.error("test_connector_method failed", connector_id=connector_id, method=method_name, error=str(e))
        return {"status": "error", "method": method_name, "error": str(e)}


@app.get("/connectors/{connector_id}/metadata")
async def get_connector_metadata(
    connector_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    """Return the full connector.json metadata for a deployed connector."""
    connector = registry.get(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    generated_root = Path(os.getenv("GENERATED_CODE_DIR") or _DEFAULT_GENERATED_DIR).resolve()
    connector_type = connector.CONNECTOR_TYPE

    for tenant_dir in generated_root.iterdir():
        if not tenant_dir.is_dir() or tenant_dir.name != tenant_id:
            continue
        for pkg_dir in tenant_dir.iterdir():
            meta_file = pkg_dir / "metadata" / "connector.json"
            if meta_file.exists():
                try:
                    candidate = json.loads(meta_file.read_text())
                    if candidate.get("connector_type") == connector_type:
                        return candidate
                except Exception:
                    pass

    raise HTTPException(status_code=404, detail="connector.json not found for this connector")


@app.get("/connectors/oauth/callback")
async def oauth_redirect_callback(request: Request):
    """Google (and other OAuth providers) redirect here after user grants permission.

    Uses window.postMessage to automatically send the code back to the opener
    (CMS deploy page). No manual copy-paste needed — the popup closes itself
    after posting the message.
    """
    from fastapi.responses import HTMLResponse
    code  = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    error = request.query_params.get("error", "")

    if error:
        html = f"""<!DOCTYPE html>
<html><head><title>Authorization Failed</title></head>
<body style="font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#FEF2F2">
<div style="text-align:center;padding:40px;max-width:480px">
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#DC2626" stroke-width="1.5" style="margin-bottom:16px"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
    <h2 style="color:#991B1B;margin:0 0 8px">Authorization Failed</h2>
    <p style="color:#7F1D1D;margin:0 0 4px">{error}</p>
    <p style="color:#6B7280;font-size:13px">Close this window and try again.</p>
</div>
<script>
    // Notify opener of error then close
    if (window.opener) {{
        window.opener.postMessage({{type:"oauth_callback",error:"{error}",state:"{state}"}}, "*");
        setTimeout(() => window.close(), 2000);
    }}
</script>
</body></html>"""
        return HTMLResponse(html, status_code=400)

    html = f"""<!DOCTYPE html>
<html><head><title>Authorization Successful</title></head>
<body style="font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#F0FDF4">
<div style="text-align:center;padding:40px;max-width:480px">
    <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="#0D9488" stroke-width="1.5" style="margin-bottom:16px"><circle cx="12" cy="12" r="10"/><polyline points="9 12 11 14 15 10"/></svg>
    <h2 style="color:#065F46;margin:0 0 8px">Authorization Successful</h2>
    <p style="color:#374151;font-size:14px;margin:0 0 24px">Sending you back to Shielva…</p>
    <div style="display:inline-flex;align-items:center;gap:8px;padding:8px 16px;background:#ECFDF5;border:1px solid #6EE7B7;border-radius:20px;font-size:12px;color:#065F46">
        <span style="width:8px;height:8px;border-radius:50%;background:#10B981;animation:pulse 1s ease-in-out infinite;display:inline-block"></span>
        Closing automatically…
    </div>
</div>
<style>@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.4}}}}</style>
<script>
    // Send code to parent window via postMessage, then close popup
    var payload = {{type:"oauth_callback",code:"{code}",state:"{state}"}};
    if (window.opener && !window.opener.closed) {{
        window.opener.postMessage(payload, "*");
        setTimeout(function() {{ window.close(); }}, 800);
    }} else {{
        // Opened as full tab (no opener) — show manual fallback
        document.querySelector("p").textContent = "Paste this code in the connector setup:";
        var box = document.createElement("div");
        box.style.cssText = "background:#fff;border:1.5px solid #14B8A6;border-radius:8px;padding:12px 16px;font-family:monospace;font-size:12px;word-break:break-all;color:#0F766E;margin:12px auto;max-width:400px;user-select:all";
        box.textContent = "{code}";
        document.querySelector("div").appendChild(box);
        document.querySelector(".inline-flex")?.remove();
    }}
</script>
</body></html>"""
    return HTMLResponse(html)


@app.post("/connectors/{connector_id}/callback")
async def oauth_callback(
    connector_id: str,
    request: OAuthCallbackRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Handle OAuth callback"""
    connector = registry.get(connector_id)
    
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    
    if connector.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        token_info = await connector.authorize(
            auth_code=request.code,
            state=request.state
        )

        # After successful OAuth, persist full connector config + auth hash.
        # _auth_hash is used by the check endpoint to skip re-auth when credentials unchanged.
        # Tokens are managed via connector_store — exclude them from credential_manager.
        _EXCLUDE_FROM_CONFIG = {"access_token", "refresh_token", "id_token", "token", "expires_at", "expires_in"}

        # Resolve the canonical CONNECTOR_CLASSES key for this connector instance.
        # connector.CONNECTOR_TYPE may be the short name (e.g. "gmail") while the
        # CONNECTOR_CLASSES key is the full package name (e.g. "shielva_gmail_connector").
        # Using the wrong key causes credentials to be stored under a different name than
        # what the frontend reads via GET /credentials/{connector_type}/values.
        _resolved_cred_type = next(
            (k for k, v in CONNECTOR_CLASSES.items() if v is type(connector)),
            connector.CONNECTOR_TYPE,
        )

        try:
            import hashlib as _hashlib, json as _hjson
            _AUTH_KEYS = ("client_id", "client_secret", "scopes")
            _auth_hash = _hashlib.sha256(
                _hjson.dumps(
                    {k: str((connector.config or {}).get(k, "")) for k in _AUTH_KEYS}
                    | {"connector_type": _resolved_cred_type},
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:16]

            config_to_save = {
                k: v for k, v in (connector.config or {}).items()
                if v is not None and v != "" and k not in _EXCLUDE_FROM_CONFIG
            }
            config_to_save["_auth_hash"] = _auth_hash
            if config_to_save:
                await credential_manager.store_credentials(tenant_id, _resolved_cred_type, config_to_save)
                logger.info("callback.credentials_saved_encrypted", connector_id=connector_id,
                            cred_type=_resolved_cred_type, keys=sorted(config_to_save.keys()))
        except Exception as _save_err:
            logger.warning("callback.credentials_save_failed", connector_id=connector_id, error=str(_save_err))

        # Store token under canonical key so future Check Connection can reuse it.
        # We bypass set_token()'s fire-and-forget asyncio.create_task() and call
        # connector_store directly with await so the save is guaranteed to be
        # committed to Redis before this response is returned to the client.
        try:
            from services.connector_store import connector_store as _cs
            _canonical_id = f"canonical_{_resolved_cred_type}_{tenant_id}"
            _token_payload = {
                "access_token":  token_info.access_token,
                "token_type":    token_info.token_type or "Bearer",
                "refresh_token": token_info.refresh_token,
                "expires_at":    token_info.expires_at.isoformat() if token_info.expires_at else None,
                "scope":         " ".join(token_info.scopes) if token_info.scopes else None,
                "raw":           token_info.raw,   # full provider payload — needed by connectors
            }                                      # that reconstruct Credentials from raw JSON
            await _cs.save_connector_tokens(_canonical_id, _token_payload)
            logger.info("callback.canonical_token_stored", connector_type=_resolved_cred_type,
                        canonical_id=_canonical_id)
        except Exception as _ct_err:
            logger.warning("callback.canonical_token_failed", error=str(_ct_err))

        # ── Run real test API call immediately after token exchange ─────
        # The token is now in memory (set_token was called inside authorize()).
        # health_check() dynamically tests the live API — Gmail calls
        # users().getProfile(), other connectors call their equivalent.
        test_result: dict | None = None
        try:
            health = await connector.health_check()
            test_healthy = health.health.value == "healthy"
            test_result = {
                "healthy": test_healthy,
                "auth_status": health.auth_status.value,
                "message": health.message if test_healthy else None,
                "error": health.error if not test_healthy else None,
            }
            logger.info(
                "callback.test_api_call",
                connector_id=connector_id,
                healthy=test_healthy,
                message=health.message,
            )
        except Exception as _health_err:
            logger.warning("callback.test_api_failed", connector_id=connector_id, error=str(_health_err))

        return {
            "status": "connected",
            "connector_id": connector_id,
            "message": "Authorization successful",
            "test_result": test_result,
        }

    except Exception as e:
        logger.error("OAuth callback failed", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/connectors/{connector_id}/sync", response_model=SyncResponse)
async def sync_connector(
    connector_id: str,
    request: SyncRequest,
    background_tasks: BackgroundTasks,
    tenant_id: str = Depends(get_tenant_id)
):
    """Trigger connector sync"""
    connector = registry.get(connector_id)
    
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    
    if connector.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Generate job ID
    import uuid
    job_id = str(uuid.uuid4())
    
    # Run sync in background
    async def run_sync():
        import httpx
        try:
            # Pass webhook_url to sync method
            result = await connector.sync(
                full=request.full_sync, 
                kb_id=request.kb_id, 
                webhook_url=request.webhook_url
            )
            
            if result.status.value == "failed":
                 logger.error("Sync reported failure result", connector_id=connector_id, errors=result.errors)
                 if request.webhook_url:
                     async with httpx.AsyncClient() as client:
                         await client.post(request.webhook_url, json={
                             "status": "failed",
                             "error": "; ".join(result.errors) if result.errors else "Unknown connector error",
                             "documents_processed": 0,
                             "chunks_created": 0
                         }, headers={"X-Tenant-ID": tenant_id})
                 return

            logger.info(
                "Sync completed",
                connector_id=connector_id,
                documents_synced=result.documents_synced,
                kb_id=request.kb_id
            )
        except Exception as e:
            logger.error("Sync background task crashed", connector_id=connector_id, error=str(e))
            if request.webhook_url:
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post(request.webhook_url, json={
                            "status": "failed",
                            "error": f"Connector task crash: {str(e)}",
                            "documents_processed": 0,
                            "chunks_created": 0
                        }, headers={"X-Tenant-ID": tenant_id})
                except Exception as e:
                    logger.error("Webhook notification failed", error=str(e))
    
    background_tasks.add_task(run_sync)
    
    return SyncResponse(
        job_id=job_id,
        status="syncing",
        message="Sync started in background"
    )


@app.get("/connectors/{connector_id}/status", response_model=ConnectorStatusResponse)
async def get_connector_status(
    connector_id: str,
    tenant_id: str = Depends(get_tenant_id)
):
    """Get connector status"""
    connector = registry.get(connector_id)
    
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    
    if connector.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    status = await connector.health_check()
    
    return ConnectorStatusResponse(
        connector_id=connector_id,
        connector_type=connector.CONNECTOR_TYPE,
        health=status.health.value,
        auth_status=status.auth_status.value,
        last_sync=status.last_sync,
        documents_indexed=status.documents_indexed,
        error=status.error
    )


@app.get("/connectors")
async def list_connectors(
    tenant_id: str = Depends(get_tenant_id)
):
    """List a tenant's installed connectors.

    Source of truth is the PERSISTED connector_store, not the in-memory registry
    (which is empty after a restart or when a connector failed to re-initialize on
    boot). Reading the registry made installs disappear on refresh. We enrich with
    live health from the registry when the connector happens to be loaded.
    """
    connectors = []
    try:
        stored = await connector_store.list_connectors()
    except Exception as exc:  # noqa: BLE001 — never sink the page on a store hiccup
        logger.error("list_connectors store read failed", error=str(exc)[:200])
        stored = []
    for cfg in stored:
        if getattr(cfg, "tenant_id", None) != tenant_id:
            continue
        cid = getattr(cfg, "connector_id", None)
        if not cid:
            continue
        health, auth = "unknown", "unknown"
        conn = registry.get(cid)
        if conn:
            status = conn.get_status()
            health, auth = status.health.value, status.auth_status.value
        connectors.append({
            "connector_id": cid,
            "connector_type": getattr(cfg, "connector_type", None),
            "health": health,
            "auth_status": auth,
        })
    return {"connectors": connectors}


@app.delete("/connectors/{connector_id}")
async def delete_connector(
    connector_id: str,
    tenant_id: str = Depends(get_tenant_id)
):
    """Delete a connector"""
    connector = registry.get(connector_id)

    # Connector may not be in registry after server restart — look up from store
    if not connector:
        stored = await connector_store.get_connector(connector_id)
        if not stored:
            raise HTTPException(status_code=404, detail="Connector not found")
        if stored.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        connector_type_for_creds = stored.connector_type
    else:
        if connector.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        connector_type_for_creds = connector.CONNECTOR_TYPE
        # Close connector
        if hasattr(connector, 'close'):
            await connector.close()
        # Remove from registry
        registry.remove(connector_id)

    # Remove from persistence
    await connector_store.delete_connector(connector_id)

    # Delete stored credentials so the form starts fresh on next install
    if connector_type_for_creds:
        try:
            await credential_manager.delete_credentials(tenant_id, connector_type_for_creds)
        except Exception as _e:
            logger.warning("uninstall.creds_delete_failed", connector_id=connector_id, error=str(_e))

    return {"status": "deleted", "connector_id": connector_id}


@app.post("/connectors/{connector_id}/clear-auth")
async def clear_connector_auth(
    connector_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    """Clear stored OAuth tokens and auth hash for a connector so the next
    Check Connection forces a fresh OAuth popup."""
    connector = registry.get(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Delete per-connector token
    await connector_store.delete_connector_tokens(connector_id)

    # Delete canonical token for this connector type + tenant so smart re-auth
    # cannot find a cached token from a previous session.
    _canonical_id = f"canonical_{connector.CONNECTOR_TYPE}_{tenant_id}"
    await connector_store.delete_connector_tokens(_canonical_id)

    # Reset the in-memory token so the live connector instance also forgets it
    if hasattr(connector, "_token_info"):
        connector._token_info = None

    return {"status": "cleared", "connector_id": connector_id}


# ===== Webhook Endpoints =====

@app.post("/webhooks/{connector_type}/{connector_id}")
async def handle_webhook(
    connector_type: str,
    connector_id: str,
    request: Request
):
    """Handle incoming webhooks from external services"""
    connector = registry.get(connector_id)
    
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    
    payload = await request.json()
    
    try:
        await connector.handle_webhook(payload)
        return {"status": "processed"}
    except Exception as e:
        logger.error("Webhook processing failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ===== Scheduler Integration =====
from services.scheduler import ConnectorScheduler

# Initialize scheduler (global)
scheduler = ConnectorScheduler()

@app.post("/connectors/{connector_id}/schedule/start")
async def start_schedule(
    connector_id: str,
    payload: Dict[str, Any], # kb_id, interval, webhook_url
    tenant_id: str = Depends(get_tenant_id)
):
    """Start auto-sync schedule"""
    connector = registry.get(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
        
    if connector.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
        
    kb_id = payload.get("kb_id")
    interval = payload.get("interval_seconds", 10)
    webhook_url = payload.get("webhook_url")
    
    if not kb_id:
        raise HTTPException(status_code=400, detail="Missing kb_id")
        
    scheduler.schedule_connector(
        connector_id=connector_id,
        kb_id=kb_id,
        interval_seconds=interval,
        webhook_url=webhook_url
    )
    
    # NEW: Persist schedule intent to ConnectorStore
    await connector_store.save_connector(
        connector_id=connector_id,
        connector_type=connector.CONNECTOR_TYPE,
        tenant_id=tenant_id,
        config=connector.config,
        schedule_interval=interval,
        kb_id=kb_id
    )
    
    return {"status": "scheduled", "interval": interval}

@app.post("/connectors/{connector_id}/schedule/stop")
async def stop_schedule(
    connector_id: str,
    payload: Dict[str, Any], # kb_id (optional validation)
    tenant_id: str = Depends(get_tenant_id)
):
    """Stop auto-sync schedule"""
    # Verify ownership
    connector = registry.get(connector_id)
    if not connector:
        # If connector logic is gone but job remains?
        # We should allow stopping even if connector instance missing (e.g. restart) 
        # But we need tenant verification. 
        # If registry empty, we can't verify tenant easily without DB lookup.
        # Let's assume connector is loaded (as per lifespan).
        pass

    if connector and connector.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
        
    scheduler.unschedule_connector(connector_id)
    
    # NEW: Persist removal of schedule to ConnectorStore
    if connector:
        await connector_store.save_connector(
            connector_id=connector_id,
            connector_type=connector.CONNECTOR_TYPE,
            tenant_id=tenant_id,
            config=connector.config,
            schedule_interval=None,
            kb_id=None
        )
    
    return {"status": "stopped"}

@app.get("/connectors/{connector_id}/schedule")
async def get_schedule(
    connector_id: str,
    tenant_id: str = Depends(get_tenant_id)
):
    """Get schedule status"""
    connector = registry.get(connector_id)
    if connector and connector.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
        
    status = scheduler.get_job_status(connector_id)
    if status.get("status") == "inactive":
        # Fallback to persistent metadata
        config = await connector_store.get_connector(connector_id)
        if config and config.schedule_interval:
            return {
                "status": "active",
                "next_run": "Restoring...",
                "interval": f"interval[{config.schedule_interval}s]"
            }
            
    return status


# ===== Run Server =====

def main():
    """Run the connector gateway"""
    import multiprocessing
    import os
    
    host = "0.0.0.0"
    port = int(os.getenv("CONNECTORS_PORT", 8003))
    debug = os.getenv("ENVIRONMENT") == "development"
    
    if debug:
        print(f"🔌 Starting Connector Gateway in DEVELOPMENT mode (port {port})")
        # Serve HTTPS when the dev cert/key are present. The whole local stack runs
        # TLS with the localhost cert and callers reach this gateway at
        # https://localhost:8003 (platform-core CONNECTORS_URL / CONNECTOR_BASE_URL).
        # Without TLS here, those calls fail the handshake and the auto-sync /
        # resync / schedule endpoints time out at the gateway (504). Falls back to
        # plain HTTP when no certs are set.
        ssl_certfile = os.getenv("CERT_FILE") or os.getenv("SSL_CERTFILE")
        ssl_keyfile = os.getenv("KEY_FILE") or os.getenv("SSL_KEYFILE")
        run_kwargs = {"host": host, "port": port, "reload": True}
        if ssl_certfile and ssl_keyfile and os.path.exists(ssl_certfile) and os.path.exists(ssl_keyfile):
            run_kwargs["ssl_certfile"] = ssl_certfile
            run_kwargs["ssl_keyfile"] = ssl_keyfile
        uvicorn.run("gateway:app", **run_kwargs)
    else:
        worker_count = int(os.getenv("WORKERS", (multiprocessing.cpu_count() * 2) + 1))
        # Scheduler inside gunicorn workers is tricky (multiple schedulers!).
        # For auto-ingestion:
        # Ideally, scheduler runs in ONE process (e.g. a separate service or using a lock).
        # APScheduler RedisJobStore handles distributed locking somewhat, but having 10 workers submitting jobs 
        # or trying to run them might be an issue if they don't share the same event loop.
        # Actually, RedisJobStore is for storage. The Execution happens in the process that started the scheduler.
        # If we run 5 workers, and all start scheduler, all 5 will try to pick up jobs.
        # Valid for robustness, but might cause duplicate execution if not locked.
        # APScheduler does NOT support distributed locking out of the box for execution.
        # Recommendation: Run scheduler in a dedicated process or single worker.
        # For simplicity in this script: We assume 1 worker or we accept redundancy (jobs are idempotent-ish).
        # OR: We rely on the fact that we use "development" mode mostly or single replica in k8s.
        
        print(f"🔌 Starting Connector Gateway in PRODUCTION mode with {worker_count} workers (port {port})")
        
        options = {
            "bind": f"{host}:{port}",
            "workers": worker_count,
            "worker_class": "uvicorn.workers.UvicornWorker",
            "timeout": 60,
        }
        
        from gunicorn.app.base import BaseApplication

        class StandaloneApplication(BaseApplication):
            def __init__(self, app, options=None):
                self.options = options or {}
                self.application = app
                super().__init__()

            def load_config(self):
                config = {key: value for key, value in self.options.items()
                          if key in self.cfg.settings and value is not None}
                for key, value in config.items():
                    self.cfg.set(key.lower(), value)

            def load(self):
                return self.application

        StandaloneApplication(app, options).run()


if __name__ == "__main__":
    main()
