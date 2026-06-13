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
    root = Path(os.getenv("GENERATED_CODE_DIR", "generated_connectors")).resolve()
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


def _load_generated_connectors(generated_root: str = None) -> None:
    """Dynamically discover and register AI-generated connectors from generated_connectors/.

    Scans generated_connectors/{tenant_id}/{service_slug}_connector/connector.py,
    imports the module, finds the BaseConnector subclass, and adds it to CONNECTOR_CLASSES
    keyed by its CONNECTOR_TYPE attribute.

    Called at startup so generated connectors are available immediately.
    """
    import importlib.util

    root = Path(generated_root or os.getenv("GENERATED_CODE_DIR", "generated_connectors")).resolve()
    if not root.exists():
        logger.info("generated_connectors directory not found — skipping dynamic load", path=str(root))
        return

    loaded = 0
    for tenant_dir in root.iterdir():
        if not tenant_dir.is_dir():
            continue
        for pkg_dir in tenant_dir.iterdir():
            if not pkg_dir.is_dir() or not pkg_dir.name.endswith("_connector"):
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

            try:
                # Add package root and shared path to sys.path if needed
                connectors_root = str(Path(__file__).resolve().parent)
                if connectors_root not in sys.path:
                    sys.path.insert(0, connectors_root)

                # Also add the connector's own directory so that
                # `from repository.X import Y`, `from client.X import Y` etc.
                # resolve against THIS connector's local packages.
                pkg_dir_str = str(pkg_dir.resolve())
                if pkg_dir_str not in sys.path:
                    sys.path.insert(0, pkg_dir_str)

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
                        # Also register as bare name so unqualified import works
                        sys.modules.setdefault(subpkg, subpkg_mod)
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
                            sys.modules.setdefault(f"{subpkg}.{child_py.stem}", child_mod)
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

    root = Path(generated_root or os.getenv("GENERATED_CODE_DIR", "generated_connectors")).resolve()
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
            if not pkg_dir.is_dir() or not pkg_dir.name.endswith("_connector"):
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

    # Load AI-generated connectors dynamically
    _load_generated_connectors()

    # HA: subscribe to the cluster-wide connector-reload channel so a deploy/reload
    # on any pod fans out to this one (no stale/404 connectors across replicas).
    app.state.reload_sub = asyncio.create_task(_connector_reload_subscriber())

    # Startup: Restore Connectors
    stored_connectors = await connector_store.list_connectors()
    for config in stored_connectors:
        try:
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

    user_role = (request.headers.get("X-User-Role") or "viewer").lower()
    if user_role not in ("super_admin", "tenant_admin", "admin"):
        raise HTTPException(
            status_code=403,
            detail=f"Role '{user_role}' cannot trigger pull-and-reload. Requires super_admin or tenant_admin.",
        )

    result = {"git_pull": None, "reload": None}
    repo_root = Path(__file__).resolve().parent  # shielva-connectors/

    # ── Step 1: git pull using PAT-authenticated HTTPS URL ──────────────────
    try:
        owner, repo = _parse_repo_for_pull(body.github_repo_url)
        # Build authenticated HTTPS remote: https://<PAT>@github.com/owner/repo.git
        auth_remote = f"https://{body.github_token}@github.com/{owner}/{repo}.git"

        proc = await _asyncio.to_thread(
            _sp.run,
            ["git", "pull", "--ff-only", auth_remote, body.target_branch],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (proc.stdout + proc.stderr).strip()
        # Sanitize — never include the PAT in responses or logs
        safe_output = output.replace(body.github_token, "***")

        if proc.returncode == 0:
            result["git_pull"] = "success"
            logger.info("pull_and_reload.git_pull_success", branch=body.target_branch, output=safe_output[:200])
        else:
            result["git_pull"] = f"failed: {safe_output[:200]}"
            logger.error("pull_and_reload.git_pull_failed", branch=body.target_branch, output=safe_output[:500])
    except ValueError as e:
        result["git_pull"] = f"invalid repo URL: {str(e)[:100]}"
    except Exception as e:
        result["git_pull"] = f"error: {str(e)[:100]}"
        logger.error("pull_and_reload.git_pull_error", error=str(e)[:200])

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
    """List available connector types"""
    # Legacy hardcoded vendor connectors removed — connectors are authored via the
    # Integration Builder and loaded dynamically into CONNECTOR_CLASSES. Surface the
    # live (generated) connectors plus the remaining coming-soon placeholders.
    generated = [
        {"type": ct, "name": ct, "description": "Integration Builder connector", "auth_type": "oauth2"}
        for ct in sorted(CONNECTOR_CLASSES.keys())
    ]
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
        # If redirect_uri is provided in config, use it. 
        # Otherwise, let the connector use its internal default if it has one.
        redirect_uri = final_config.get("redirect_uri")
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
    _gw = os.getenv("GATEWAY_URL", "https://localhost:8000")
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
    """Deploy an AI-generated connector: load from generated_connectors, install it,
    and return the connector_id + oauth_url (if OAuth) or connected status (if API key).

    Body:
        connector_type: str   — matches CONNECTOR_TYPE in connector.py
        session_id: str       — integration builder session ID (for metadata lookup)
        config: dict          — install-time credentials/settings
    """
    import uuid

    body = await request.json()
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
                    connector_type = _meta.get("connector_type", connector_type)
        except Exception as _ex:
            logger.warning("deploy.connector_type_resolution_failed", session_id=session_id, error=str(_ex))

    if not connector_type:
        raise HTTPException(status_code=400, detail="connector_type is required and could not be resolved from session_id")

    # Re-scan generated connectors in case a new one was just built
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
        await connector.initialize()  # pulls token from Redis into memory
        health = await connector.health_check()
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
        # Token not yet stored (OAuth still pending) — not a fatal error
        logger.warning("deploy.test_api_skipped", connector_id=connector_id, reason=str(_health_err))
        test_result = None

    # If still pending after test (no token yet), generate OAuth URL
    effective_status = status.auth_status.value
    if test_result and test_result["healthy"]:
        effective_status = "connected"
    elif test_result and not test_result["healthy"] and test_result["auth_status"] not in ("pending", "missing_credentials"):
        effective_status = test_result["auth_status"]

    if effective_status in ("pending", "missing_credentials") or (test_result is None and status.auth_status.value == "pending"):
        _gw = os.getenv("GATEWAY_URL", "https://localhost:8000")
        _default_redirect = f"{_gw}/connectors/oauth/callback"
        redirect_uri = final_config.get("redirect_uri") or _default_redirect
        # Persist redirect_uri in connector config so authorize() can use it later
        connector.config["redirect_uri"] = redirect_uri
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

    # Try reading connector.json from generated_connectors
    generated_root = Path(os.getenv("GENERATED_CODE_DIR", "generated_connectors")).resolve()
    connector_type = connector.CONNECTOR_TYPE

    # Search all tenant dirs for this connector_type
    metadata: dict | None = None
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
        raw_apis = metadata.get("apis", [])
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
            normalized_apis.append({**api, "params": normalized_params})
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
    import inspect, dataclasses

    connector = registry.get(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
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

    generated_root = Path(os.getenv("GENERATED_CODE_DIR", "generated_connectors")).resolve()
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
    """List all connectors for tenant"""
    connectors = []
    
    for connector_id in registry.list_all():
        connector = registry.get(connector_id)
        if connector and connector.tenant_id == tenant_id:
            status = connector.get_status()
            connectors.append({
                "connector_id": connector_id,
                "connector_type": connector.CONNECTOR_TYPE,
                "health": status.health.value,
                "auth_status": status.auth_status.value
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
