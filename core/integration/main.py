"""Shielva Integration Builder — FastAPI Application.

AI-assisted connector code generation service.
Port: 8055 (default)
"""

# Load core/.env (MASTER_KEY etc.) into the process env BEFORE any module reads
# os.getenv at import time. override=False so server.sh-exported vars take priority.
from dotenv import load_dotenv
from pathlib import Path as _EnvPath
# Load core/.env by EXPLICIT path — NOT load_dotenv()'s auto-discovery, which walks
# up from this file (core/integration/) and stops at core/integration/.env, missing
# the shared secrets (MASTER_KEY, CONNECTOR_INTERNAL_TOKEN) that live in core/.env.
_core_dir = _EnvPath(__file__).resolve().parent.parent  # shielva-connectors/core
load_dotenv(_core_dir / ".env", override=False)                       # shared secrets
load_dotenv(_core_dir / "integration" / ".env", override=False)       # integration-specific vars

# Decrypt vault:v1: sealed secrets BEFORE config reads env (VAULT_ENVELOPE_DIRECT
# → AppRole-login + transit-decrypt against shielva-vault). No-op when unset.
from shielva_common.envelope import bootstrap as _envelope_bootstrap

_envelope_bootstrap()

import asyncio
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from integration.api.catalog_routes import catalog_router
from integration.api.catalog_v3_routes import catalog_v3_router
from integration.api.categories_routes import categories_router
from integration.api.codegen_routes import codegen_router
from integration.api.codeview_routes import codeview_router
from integration.api.history_routes import history_router
from integration.api.logs_routes import logs_router
from integration.api.planning_routes import planning_router
from integration.api.session_routes import session_router
from integration.api.testing_routes import testing_router
from integration.api.connector_api_routes import connector_api_router
from integration.api.guidelines_routes import guidelines_router
from integration.api.knowledge_routes import knowledge_router
from integration.api.docs_routes import docs_router
from integration.api.ws_routes import ws_router
from integration.api.step_prompts_routes import step_prompts_router
from integration.api.models_routes import models_router
from integration.api.instructions_routes import instructions_router
from integration.api.terminal_routes import terminal_router
from integration.api.prompt_steps_routes import prompt_steps_router
from integration.core.config import settings
from integration.db.database import close_db, connect_db
from integration.services import r2_service
from integration.services.guidelines_service import seed_default_guidelines, seed_test_case_writing_guidelines
from integration.services.docs_guidelines_service import seed_default_doc_guidelines
from integration.services.metadata_guidelines_service import seed_metadata_writing_guidelines
from integration.services.instructions_guidelines_service import seed_instruction_guidelines
from integration.services.shared_venv import setup_shared_venv_async
from integration.api.system_routes import system_router
from integration.api.sync_request_routes import sync_request_router
from integration.api.sync_webhook_routes import sync_webhook_router


# ── Logging setup ────────────────────────────────────────────────────
# LOG_LEVEL from config — NEVER "debug"/"trace" in production (SOC 2 CC7.2 / C1.1).

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# Configure stdlib routing so uvicorn / third-party loggers write via structlog.
logging.basicConfig(
    format="%(message)s",
    level=logging.getLevelName(settings.LOG_LEVEL.upper()),
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "integration-builder.log", encoding="utf-8"),
    ],
)

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        # add_logger_name requires a stdlib logger (.name attr) — use
        # stdlib.LoggerFactory(), not PrintLoggerFactory() (structlog ≥24.1).
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.LOG_LEVEL.upper())
    ),
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

logger = structlog.get_logger(__name__)


# ── Request/Response logging middleware ──────────────────────────────

class TenantBucketMiddleware(BaseHTTPMiddleware):
    """Sets R2 bucket ContextVars from request headers (or query params for SSE) on every request.

    Priority (highest → lowest):
      1. X-App-ID header  → "shielva-agentic-app-{app_id}" — stable per-installation bucket.
                            Covers pre-login AND post-login sessions for the same device.
      2. app_id query param → same bucket name (fallback for EventSource / SSE connections
                              which cannot send custom headers).
      3. X-Tenant-Name header → tenant-root bucket (legacy / multi-tenant server deployments).
      4. tenant_id query param → same as above (SSE fallback).

    The configured R2_BUCKET_NAME env var always overrides all of the above (see r2_service._get_bucket).
    """

    async def dispatch(self, request: Request, call_next):
        # Headers (standard API calls)
        app_id      = request.headers.get("X-App-ID", "").strip()
        tenant_name = request.headers.get("X-Tenant-Name", "").strip().lower()

        # Query param fallback (SSE / EventSource connections can't send custom headers)
        if not app_id:
            app_id = request.query_params.get("app_id", "").strip()
        if not tenant_name:
            tenant_name = request.query_params.get("tenant_id", "").strip().lower()

        tokens = []
        if app_id:
            bucket = r2_service.app_id_to_bucket(app_id)
            tokens.append(r2_service._app_bucket_ctx.set(bucket))
        if tenant_name:
            tokens.append(r2_service._tenant_bucket_ctx.set(tenant_name))

        try:
            return await call_next(request)
        finally:
            for t in tokens:
                # Each ContextVar token has a .var attribute we use to reset
                try:
                    t.var.reset(t)
                except Exception:
                    pass


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs every HTTP request with method, path, status, and duration.

    Reads ``X-Correlation-Id`` from the inbound request (or generates a fresh
    UUID hex) and binds it into structlog contextvars so every log line emitted
    during that request carries ``correlation_id`` automatically.  The value is
    also echoed back in the response header.
    """

    async def dispatch(self, request: Request, call_next):
        # Prefer inbound X-Correlation-Id; fall back to a new UUID hex.
        correlation_id = request.headers.get("X-Correlation-Id") or uuid.uuid4().hex
        request_id = str(uuid.uuid4())[:8]
        start = time.perf_counter()

        # Bind correlation_id + request_id to structlog context for this request.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            request_id=request_id,
        )

        logger.info(
            "http.request_started",
            method=request.method,
            path=str(request.url.path),
            query=str(request.url.query) if request.url.query else None,
            client=request.client.host if request.client else None,
            tenant_id=request.headers.get("x-tenant-id"),
        )

        try:
            response: Response = await call_next(request)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.error(
                "http.request_failed",
                method=request.method,
                path=str(request.url.path),
                error=str(exc),
                duration_ms=duration_ms,
            )
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        log_method = logger.warning if response.status_code >= 400 else logger.info
        log_method(
            "http.request_completed",
            method=request.method,
            path=str(request.url.path),
            status=response.status_code,
            duration_ms=duration_ms,
            tenant_id=request.headers.get("x-tenant-id"),
        )

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Correlation-Id"] = correlation_id
        return response


# ── Lifespan ──────────────────────────────────────────────────────────

async def _recover_stale_sessions() -> None:
    """On startup, flip any sessions stuck in 'executing' to 'failed'.

    When the server crashes mid-execution the session status is never updated —
    it stays 'executing' forever. Without this sweep the WS reconnect logic
    re-attaches to a ghost execution that will never complete.
    """
    from datetime import datetime
    from integration.db.database import sessions_collection as _sc
    try:
        col = _sc()
        result = await col.update_many(
            {"status": "executing"},
            {"$set": {
                "status": "failed",
                "error": "Server restarted — execution interrupted. Please re-run.",
                "updated_at": datetime.utcnow(),
            }},
        )
        if result.modified_count:
            logger.warning(
                "integration_builder.stale_sessions_recovered",
                count=result.modified_count,
            )
    except Exception as _e:
        logger.error("integration_builder.stale_session_recovery_failed", error=str(_e))


async def _session_watchdog() -> None:
    """Background task: every 5 min, sweep for sessions stuck in 'executing' > 45 min.

    Guards against crashes or code paths that forget to flip status on error.
    Also catches hanging Gemini API calls that exceed the 300 s httpx timeout.
    """
    from datetime import datetime, timedelta
    from integration.db.database import sessions_collection as _sc
    while True:
        try:
            await asyncio.sleep(300)  # check every 5 minutes
            cutoff = datetime.utcnow() - timedelta(minutes=45)
            col = _sc()
            result = await col.update_many(
                {"status": "executing", "updated_at": {"$lt": cutoff}},
                {"$set": {
                    "status": "failed",
                    "error": "Execution watchdog: timed out after 45 min with no progress.",
                    "updated_at": datetime.utcnow(),
                }},
            )
            if result.modified_count:
                logger.warning(
                    "integration_builder.watchdog_recovered",
                    count=result.modified_count,
                )
        except asyncio.CancelledError:
            break
        except Exception as _e:
            logger.error("integration_builder.watchdog_error", error=str(_e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("integration_builder.starting", port=settings.INTEGRATION_PORT)
    await connect_db()
    r2_service.ensure_bucket()
    # Set up shared Python 3.13 venv with common deps pre-installed
    await setup_shared_venv_async()
    # Recover sessions stuck in 'executing' from a previous crash
    await _recover_stale_sessions()
    await seed_default_guidelines()               # seeds CODE_EXECUTION_GUIDELINES on first boot
    await seed_test_case_writing_guidelines()     # seeds TEST_CASE_WRITING_GUIDELINES.md to R2 on first boot
    await seed_default_doc_guidelines()           # seeds CONNECTOR_DOCUMENTATION_GUIDELINES on first boot
    await seed_metadata_writing_guidelines()      # seeds METADATA_WRITING_GUIDELINES on first boot
    await seed_instruction_guidelines()           # seeds INSTRUCTION_SETUP_GUIDELINES on first boot
    # Upload step prompts to R2/local on first boot (skips if already present so manual R2 edits are preserved)
    await r2_service.sync_all_step_prompts_to_r2()
    # Ensure MongoDB indexes for sync request collections
    from integration.api.sync_request_routes import ensure_sync_indexes, close_gh_client
    await ensure_sync_indexes()
    # Provider category taxonomy + mapping: ensure indexes then seed from
    # connector_catalog.json on first boot. Both ops are idempotent —
    # restarts are cheap, manual edits are never overwritten.
    from integration.services import category_service as _category_service
    await _category_service.ensure_indexes()
    await _category_service.seed_categories_from_json()
    # Background watchdog — heals sessions that get stuck mid-execution
    watchdog_task = asyncio.create_task(_session_watchdog())

    # Register with the API gateway so proxy routing works
    import sys as _sys
    import os as _os
    _connectors_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if _connectors_root not in _sys.path:
        _sys.path.insert(0, _connectors_root)
    from shielva_common.discovery_client import DiscoveryClient
    ssl_up = bool(settings.SSL_CERTFILE and settings.SSL_KEYFILE)
    scheme = "https" if ssl_up else "http"
    app.state.discovery = DiscoveryClient(
        service_name="integration",
        service_port=settings.INTEGRATION_PORT,
        gateway_url=settings.API_GATEWAY_URL,
        scheme=scheme,
    )
    asyncio.create_task(app.state.discovery.start())
    logger.info("integration_builder.discovery_started", gateway=settings.API_GATEWAY_URL, scheme=scheme)

    yield

    watchdog_task.cancel()
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass
    if hasattr(app.state, "discovery"):
        await app.state.discovery.stop()
    await close_gh_client()  # Close persistent GitHub API client
    await close_db()
    logger.info("integration_builder.stopped")


# ── App ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Shielva Integration Builder",
    description="AI-assisted connector code generation and integration testing",
    version="0.1.0",
    lifespan=lifespan,
)

# Global exception handlers — structured JSON error envelope for all error classes.
from integration.core.error_handlers import install_exception_handlers  # noqa: E402
install_exception_handlers(app)

# SOP observability — traces + /sop-metrics. Single SDK call wires the
# Prometheus middleware ahead of the per-request stack below.
from shielva_common.sop_sdk import setup_sop
setup_sop(app, service_name="shielva-integration-builder")

# Middleware (order matters: first added = outermost)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(TenantBucketMiddleware)

# Routers
_V3 = "/api/v3"

app.include_router(catalog_router)       # legacy: /catalog/... (keep for backward compat)
app.include_router(catalog_v3_router)    # /api/v3/catalog/... — registered first so its
                                         # GET overrides (logo_url injection, etc.) win
                                         # over the legacy router below.
# Also expose catalog_router under /api/v3 so PATCH /api/v3/catalog/providers/{key}
# and the other write endpoints (POST/DELETE custom-providers, /detail, etc.) are
# reachable from the same v3 base path the frontends use.
app.include_router(catalog_router, prefix=_V3)
app.include_router(categories_router)    # /api/v3/catalog/categories + provider mapping

# All functional routers mounted under /api/v3
app.include_router(session_router,        prefix=_V3)   # /api/v3/sessions/...
app.include_router(planning_router,       prefix=_V3)   # /api/v3/sessions/{id}/plan/...
app.include_router(codegen_router,        prefix=_V3)   # /api/v3/sessions/{id}/execute/...
app.include_router(codeview_router,       prefix=_V3)   # /api/v3/sessions/{id}/files/...
app.include_router(testing_router,        prefix=_V3)   # /api/v3/sessions/{id}/test/...
app.include_router(logs_router,           prefix=_V3)   # /api/v3/logs/...
app.include_router(connector_api_router,  prefix=_V3)   # /api/v3/connector-api/...
app.include_router(history_router,        prefix=_V3)   # /api/v3/catalog/{provider}/{service}/history
app.include_router(models_router,         prefix=_V3)   # /api/v3/models/...
from integration.api.entity_routes import entity_router
app.include_router(entity_router,         prefix=_V3)
app.include_router(guidelines_router,     prefix=_V3)
app.include_router(step_prompts_router,   prefix=_V3)
app.include_router(knowledge_router,      prefix=_V3)
app.include_router(docs_router,           prefix=_V3)
app.include_router(instructions_router,   prefix=_V3)
app.include_router(terminal_router)       # WebSocket — has hardcoded path, skip prefix
app.include_router(prompt_steps_router,   prefix=_V3)
app.include_router(system_router,         prefix=_V3)   # /api/v3/system/...
app.include_router(sync_request_router,   prefix=_V3)   # /api/v3/sync-requests/...
app.include_router(sync_webhook_router,   prefix=_V3)   # /api/v3/sync-webhooks/...
app.include_router(ws_router)

# ── Backward-compat root-level aliases ────────────────────────────────────────
# The CMS frontend calls /sessions/..., /logs/..., /connector-api/... etc.
# directly (without the /api/v3 prefix). Mirror every functional router at root
# level so those calls continue to work alongside the /api/v3 paths used by the
# agentic-developer Electron app.
_COMPAT_ROUTERS = [
    session_router,
    planning_router,
    codegen_router,
    codeview_router,
    testing_router,
    logs_router,
    connector_api_router,
    history_router,
    models_router,
    entity_router,
    guidelines_router,
    step_prompts_router,
    knowledge_router,
    docs_router,
    instructions_router,
    prompt_steps_router,
    sync_request_router,
    sync_webhook_router,
]
for _r in _COMPAT_ROUTERS:
    app.include_router(_r)


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.SERVICE_NAME}


@app.get("/api/v3/heartbeat")
async def heartbeat(request: Request):
    """SSE heartbeat stream — sends a ping every 5 seconds.
    Frontend subscribes once; connection alive = backend up, connection lost = backend down.
    """
    import json
    from fastapi.responses import StreamingResponse

    async def _stream():
        try:
            while True:
                # Check if client disconnected before sending the next ping
                if await request.is_disconnected():
                    break
                ts = int(asyncio.get_running_loop().time() * 1000)
                yield f"event: ping\ndata: {json.dumps({'ts': ts})}\n\n"
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Entrypoint ────────────────────────────────────────────────────────

if __name__ == "__main__":
    ssl_kwargs = {}
    if settings.SSL_CERTFILE and settings.SSL_KEYFILE:
        ssl_kwargs["ssl_certfile"] = settings.SSL_CERTFILE
        ssl_kwargs["ssl_keyfile"] = settings.SSL_KEYFILE

    uvicorn.run(
        "integration.main:app",
        host="0.0.0.0",
        port=settings.INTEGRATION_PORT,
        reload=True,
        ws_ping_interval=30,   # Send WS ping every 30s (default: 20)
        ws_ping_timeout=60,    # Wait 60s for pong before disconnect (default: 20)
        **ssl_kwargs,
    )
