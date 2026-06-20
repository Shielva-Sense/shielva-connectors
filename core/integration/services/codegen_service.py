"""Integration Builder — Code generation orchestration service.

Executes an approved plan step-by-step, publishing progress events
via an async generator (consumed as SSE by the API layer).
"""

import asyncio
import json
import time
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

import structlog
from bson import ObjectId

from integration.core.config import settings
from integration.data.catalog import get_service_detail
from integration.db.database import sessions_collection
from integration.schemas.models import (
    GeneratedFile,
    SessionStatus,
    StepExecutionResult,
    StepStatus,
)
from integration.services.code_quality import analyze_directory
from integration.services.step_executor import FIX_HANDLERS, FIX_HANDLERS_STRUCTURAL, STEP_HANDLERS, _output_dir, _append_timeline, validate_generated_files, validate_step_output, _sync_init_with_connector
from integration.services import r2_service
from integration.services import failure_tracker


# ── R2 offload for per-step execution output ──────────────────────────
# `execution_results[].output` and `execution_results[].logs` used to live
# embedded in the Mongo session document. A single connector run that retried
# steps could push that field past 100–500 KB per session, multiplied across
# 130 sessions made every list query haul 1.8 MB even with Mongo projection.
#
# These helpers split each execution result into:
#   • R2 payload  →  {step_index}.json  (stdout/stderr + logs, gzipped)
#   • Mongo slim  →  {step_index, status, duration_ms, started_at, finished_at,
#                     r2_offloaded: True}
# The logs endpoint reads the slim row + lazily fetches R2 when the UI opens
# that step's panel.

_EXEC_META_KEYS = {"step_index", "status", "duration_ms", "started_at", "finished_at"}


async def _offload_exec_result_to_r2(
    *,
    provider: str,
    service_slug: str,
    session_id: str,
    result_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """Split a fat execution-result dict into (R2 payload, Mongo slim row).

    Side-effect: writes the heavy payload to R2 (best-effort — failures fall
    through to a Mongo row that still contains the data, so we never lose
    output).  Returns the slim dict that callers should `$push` into Mongo.
    """
    payload = {
        "output": result_dict.get("output", "") or "",
        "logs": result_dict.get("logs", []) or [],
    }
    # Stash any extra heavy fields the caller added (e.g. command, cwd, exit_code).
    for k, v in list(result_dict.items()):
        if k in _EXEC_META_KEYS or k in payload:
            continue
        payload[k] = v

    slim: Dict[str, Any] = {k: result_dict[k] for k in _EXEC_META_KEYS if k in result_dict}
    # `step_index` is the only field the UI keys on, so guard its presence.
    if "step_index" not in slim:
        return result_dict  # nothing to offload safely — fall back to full row

    has_heavy = bool(payload.get("output")) or bool(payload.get("logs")) or any(
        k for k in payload if k not in ("output", "logs")
    )
    if not has_heavy:
        return slim  # no body to upload — slim row is the whole record

    try:
        step_index = int(slim["step_index"])
        await r2_service.save_step_output(
            provider=provider,
            service_slug=service_slug,
            session_id=session_id,
            step_index=step_index,
            payload=payload,
        )
        slim["r2_offloaded"] = True
    except Exception as exc:
        # Upload failed — fall back to the legacy fat row so we don't lose data.
        logger.warning(
            "execution.r2_offload_failed",
            session_id=session_id,
            step_index=slim.get("step_index"),
            error=str(exc),
        )
        return result_dict
    return slim


async def _hydrate_plan_from_r2(session: Dict[str, Any]) -> Dict[str, Any]:
    """Restore a Phase-4-slimmed plan to its full shape so step_executor can
    dispatch on per-step ``config`` / ``description`` / install_fields.

    Returns the session dict (possibly mutated) so call sites can chain. Safe
    to call on legacy sessions whose Mongo plan was never slimmed — the
    `_r2_offloaded` flag gates the work, so it's a no-op there.
    """
    plan_doc = session.get("plan")
    if not isinstance(plan_doc, dict) or not plan_doc.get("_r2_offloaded"):
        return session
    provider = session.get("provider", "")
    service_slug = session.get("service_slug") or session.get("service") or ""
    sid_val = session.get("_id")
    sid = str(sid_val) if sid_val is not None else ""
    if not provider or not service_slug or not sid:
        return session
    try:
        r2_plan = await r2_service.get_plan_full(
            provider=provider, service_slug=service_slug, session_id=sid,
        )
    except Exception as exc:
        logger.warning("codegen.plan_hydrate_failed", session_id=sid, error=str(exc))
        return session
    if not isinstance(r2_plan, dict):
        return session
    r2_by_idx = {
        s.get("index"): s for s in (r2_plan.get("steps") or [])
        if isinstance(s, dict)
    }
    merged_steps = []
    for slim in plan_doc.get("steps") or []:
        if not isinstance(slim, dict):
            merged_steps.append(slim); continue
        full = r2_by_idx.get(slim.get("index"))
        if full:
            # Mongo status wins — codegen needs the live status, not the
            # stale R2 snapshot from plan-generation time.
            merged_steps.append({**full, **slim})
        else:
            merged_steps.append(slim)
    merged = {**r2_plan, **plan_doc, "steps": merged_steps}
    merged.pop("_r2_offloaded", None)
    session["plan"] = merged
    return session


def _compute_version_suggestions(current: str) -> Dict[str, str]:
    """Given a semantic version string like '1.2.3', return patch/minor/major suggestions.
    Never includes 'current' — the version upgrade step always requires a real bump.
    """
    try:
        parts = current.split(".")
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        major, minor, patch = 1, 0, 0
    return {
        "patch": f"{major}.{minor}.{patch + 1}",
        "minor": f"{major}.{minor + 1}.0",
        "major": f"{major + 1}.0.0",
    }
from integration.services.llm_client import set_llm_tenant_id, set_llm_model
from integration.services import knowledge_service

logger = structlog.get_logger(__name__)


def _to_class_name(service: str) -> str:
    """Convert 'google_sheets' → 'GoogleSheetsConnector'."""
    return "".join(w.capitalize() for w in service.replace("-", "_").split("_")) + "Connector"


def _service_slug(provider: str, service: str) -> str:
    """Filesystem-safe slug: 'google_adsense'.

    Uses only the service name (not provider) because tenant_id already provides
    tenant isolation. Including the provider would create duplicate directories
    when the slug format changes across versions.
    """
    return service.replace("-", "_").lower()


def _slug_from_connector_name(connector_name: str) -> str:
    """Derive a filesystem-safe slug from a user-provided connector name.

    The trailing word 'connector' is stripped because _output_dir appends '_connector'
    to form the directory name — keeping it would produce a double suffix.

    Examples:
        "Shielva AWS Connector" → "shielva_aws"  → dir: shielva_aws_connector
        "My Slack Bot"          → "my_slack_bot" → dir: my_slack_bot_connector
    """
    import re
    slug = connector_name.strip().lower()
    # Strip trailing "connector" word (and any surrounding whitespace/underscores)
    slug = re.sub(r'[\s_]+connector\s*$', '', slug)
    # Replace spaces and hyphens with underscores
    slug = re.sub(r'[\s\-]+', '_', slug)
    # Strip any character that isn't alphanumeric or underscore
    slug = re.sub(r'[^\w]', '', slug)
    # Final safety: strip any residual _connector suffix after replacements
    slug = re.sub(r'_connector$', '', slug)
    return slug


def _slug_to_class_name(slug: str) -> str:
    """Convert a filesystem slug back to a PascalCase class name.

    Examples:
        "shielva_gmail" → "ShielvaGmailConnector"
        "gmail"         → "GmailConnector"
    """
    return "".join(w.capitalize() for w in slug.split("_")) + "Connector"


async def _clone_connector_dir(
    source_dir: "Path",
    dest_dir: "Path",
    old_slug: str,
    new_slug: str,
    old_name: str,
    new_name: str,
) -> None:
    """Copy a connector directory and rename all identifier references.

    Replaces inside every text file:
      • class name  : OldSlugConnector  → NewSlugConnector
      • display name: old_name          → new_name
      • module slug : old_slug          → new_slug
    """
    import shutil as _shutil

    old_class = _slug_to_class_name(old_slug)
    new_class = _slug_to_class_name(new_slug)

    _shutil.copytree(str(source_dir), str(dest_dir))

    _TEXT_EXT = {".py", ".json", ".toml", ".md", ".txt", ".yaml", ".yml", ".cfg", ".ini"}
    for fpath in sorted(dest_dir.rglob("*")):
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in _TEXT_EXT:
            continue
        try:
            content = fpath.read_text(encoding="utf-8")
            # Order: most specific → least specific
            content = content.replace(old_class, new_class)
            content = content.replace(old_name, new_name)
            content = content.replace(old_slug, new_slug)
            fpath.write_text(content, encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            pass  # skip binary / locked files


def _build_package_structure(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Build the package_structure context dict from the plan.

    The planner puts file names in the scaffold_code step's config.files list
    (as plain strings). We convert them to the {path, description} format that
    handle_write_connector / handle_write_tests expect.

    Falls back to plan.get("package_structure") for backward compatibility with
    any older sessions that stored it at the top level.
    """
    # Prefer explicit top-level field (legacy / hand-authored sessions)
    if plan.get("package_structure"):
        return plan["package_structure"]

    # Current planner format: files are in scaffold_code step config
    scaffold_files: List[Any] = []
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue  # skip malformed step entries
        if step.get("type") == "scaffold_code":
            scaffold_files = step.get("config", {}).get("files", [])
            break

    if not scaffold_files:
        return {}

    # Normalise to [{path, description}] objects.
    # Defensively strip any "{service}_connector/" prefix the LLM may have added despite
    # instructions — the output directory IS already the package root, so that prefix
    # would create a double-nested dir: …/adsense/adsense_connector/connector.py
    import re as _re

    def _strip_pkg_prefix(p: str) -> str:
        return _re.sub(r'^[^/]+_connector/', '', p)

    normalised = []
    for f in scaffold_files:
        if isinstance(f, dict):
            clean = dict(f)
            if clean.get("path"):
                clean["path"] = _strip_pkg_prefix(clean["path"])
            normalised.append(clean)
        elif isinstance(f, str):
            clean_path = _strip_pkg_prefix(f)
            normalised.append({"path": clean_path, "description": f"{clean_path} module"})

    return {"files": normalised}


# ── SSE event helpers ────────────────────────────────────────────────

# Step types that are handled by the local Claude CLI (not the server executor).
# When skip_llm=True these are treated as already-complete and skipped.
_LLM_STEP_TYPES: frozenset = frozenset({
    "write_connector",
    "write_tests",
    "generate_implementation_plan",
    "generate_test_guidelines",
    "generate_metadata",
    "setup_instructions",
})

def _sse_event(event_type: str, data: Dict[str, Any]) -> str:
    """Format a Server-Sent Event string."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


async def _heartbeat_loop(oid) -> None:
    """Update exec_heartbeat every 10s so stale-lock detection knows execution is live."""
    try:
        while True:
            await asyncio.sleep(10)
            await sessions_collection().update_one(
                {"_id": oid},
                {"$set": {"exec_heartbeat": datetime.utcnow()}},
            )
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


# ── Execute plan ─────────────────────────────────────────────────────

async def execute_plan(
    session_id: str,
    tenant_id: Optional[str],
    from_step_index: int = 0,
    force_restart: bool = False,
    skip_llm: bool = False,
) -> AsyncGenerator[str, None]:
    """Execute all approved steps in a session's plan.

    When force_restart=True (Re-Execute):
      - Ignores any existing "executing" lock (stale or live)
      - Clears ALL R2 completed-step cache
      - Resets ALL MongoDB step statuses to pending
      - Clears previous execution_results from MongoDB
      - Always runs every step from scratch

    Yields SSE-formatted strings for real-time progress.
    """
    logger.info("execution.starting", session_id=session_id, tenant_id=tenant_id,
                force_restart=force_restart)

    # Propagate tenant_id to the LLM client ContextVar so every call_llm*
    # call in this async task automatically includes it.  Required when
    # INTEGRATION_LLM_MODE=mcp (shielva-mcp needs X-Tenant-ID on every request).
    set_llm_tenant_id(tenant_id or "")

    oid = ObjectId(session_id)
    session = await sessions_collection().find_one({"_id": oid})
    if not session:
        logger.error("execution.session_not_found", session_id=session_id)
        yield _sse_event("error", {"message": f"Session {session_id} not found"})
        return

    # Phase 4: rehydrate the full plan body from R2 so step_executor sees the
    # original `config` / `description` / install_fields on every step.
    await _hydrate_plan_from_r2(session)

    # Propagate preferred Claude model to LLM client ContextVar
    set_llm_model(session.get("llm_model", "") or "")

    # ── Set R2 bucket context for the entire execution ────────────────────────
    # execute_plan() owns the session document and is the authoritative resolver.
    # Set R2 bucket context for the entire execution so every r2_service call
    # (store_implementation_plan, store_test_guidelines, save_prompt_and_plan, etc.)
    # writes to the correct bucket regardless of invocation path (WS, SSE, background task).
    #
    # Priority mirrors _get_bucket():
    #   1. app_id  → "shielva-agentic-app-{app_id}"  (per-device bucket — preferred)
    #   2. tenant_name → tenant-root bucket           (legacy / multi-tenant fallback)
    from integration.services import r2_service as _r2_svc
    _exec_app_id      = (session.get("app_id")      or "").strip()
    _exec_tenant_name = (session.get("tenant_name") or "").strip().lower()

    if _exec_app_id:
        _app_bucket = _r2_svc.app_id_to_bucket(_exec_app_id)
        _r2_svc._app_bucket_ctx.set(_app_bucket)
        logger.info("execution.bucket_ctx_set", session_id=session_id, bucket=_app_bucket, source="app_id")
    if _exec_tenant_name:
        _r2_svc._tenant_bucket_ctx.set(_exec_tenant_name)
        if not _exec_app_id:
            logger.info("execution.bucket_ctx_set", session_id=session_id, bucket=_exec_tenant_name, source="tenant_name")
    if not _exec_app_id and not _exec_tenant_name:
        logger.warning(
            "execution.bucket_unresolved",
            session_id=session_id,
            note="session has no app_id and no tenant_name — R2 writes will fall back to local disk. "
                 "Re-create the session to capture app_id.",
        )

    plan = session.get("plan", {})
    steps = plan.get("steps", [])
    if not steps:
        logger.error("execution.no_steps", session_id=session_id)
        yield _sse_event("error", {"message": "No steps in plan"})
        return

    provider = session["provider"]
    service = session["service"]
    catalog = get_service_detail(provider, service)
    if not catalog:
        _cn = session.get("connector_name") or f"{provider.title()} {service.replace('_', ' ').title()}"
        catalog = {
            "provider": provider, "service": service, "service_key": service,
            "display_name": _cn,
            "description": session.get("user_prompt") or f"Custom connector for {_cn}",
            "auth_type": session.get("auth_type") or "api_key",
            "category": "custom",
        }

    connector_name = session.get("connector_name", "")
    # Prefer the service_slug already stored in MongoDB (set on first execution).
    # Re-computing from connector_name / provider+service can produce a different slug
    # if connector_name was added/changed after the first execution, which would make
    # every step handler look in the wrong directory.
    service_slug = (
        session.get("service_slug")
        or (_slug_from_connector_name(connector_name) if connector_name else None)
        or _service_slug(provider, service)
    )
    class_name = _to_class_name(service)

    # Build shared context for all step handlers
    # step_memory accumulates live as each step completes — every handler sees what happened before
    context = {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "provider": provider,
        "service": service,
        "service_slug": service_slug,
        "service_name": catalog["display_name"],
        "connector_name": connector_name,
        "class_name": class_name,
        "auth_type": catalog.get("auth_type", "unknown"),
        "sdk_package": catalog.get("sdk_package", ""),
        "docs_url": catalog.get("docs_url", ""),
        "default_scopes": catalog.get("default_scopes", []),
        "user_prompt": session.get("user_prompt", ""),
        "llm_model": session.get("llm_model", "") or "",   # preferred Claude model for this session
        # Enhance-mode signal — every generator handler consults this. When True, the
        # handler must EDIT the seeded parent artifact (connector.py, tests, docs,
        # metadata, plan) in place rather than regenerate from scratch.
        "run_kind": session.get("run_kind", "build"),
        "parent_session_id": session.get("parent_session_id", ""),
        "is_enhance": session.get("run_kind") == "enhance",
        "package_structure": _build_package_structure(plan),
        # Running memory — updated after each step so every handler knows what happened before
        "step_memory": {
            "completed_steps": [],        # step types completed so far
            "installed_packages": [],     # populated after install_deps
            "connector_class_name": "",   # populated after write_connector
            "connector_methods": [],      # populated after write_connector
            "test_methods_covered": [],   # populated after write_tests
            "last_test_passed": 0,        # populated after run_tests
            "last_test_failed": 0,        # populated after run_tests
            "errors_encountered": [],     # any step errors, appended as they occur
            "fix_attempts": {},           # {step_type: count}
        },
    }

    # ── Build default_config binding instructions ─────────────────────────────
    _default_config: list = session.get("default_config") or []
    if _default_config:
        # Safety guard: placeholder values must NEVER be hardcoded, regardless of field name.
        # The signal is the VALUE, not the key — genuine bind:true constants always have a real
        # value (a URL, a number, a version string). Credentials always get placeholder values
        # because the LLM doesn't know the tenant's actual secret. This is connector-agnostic.
        _PLACEHOLDER_PATTERNS = (
            "YOUR_", "your_", "your-", "YOUR-", "your ", "Your ",
            "<", "FILL", "PLACEHOLDER", "EXAMPLE", "example_",
            "xxxxxx", "000000", "INSERT_", "REPLACE_",
        )

        def _is_real_constant(value: str) -> bool:
            """Return True only if the value looks like a genuine shared constant, not a placeholder."""
            v = str(value).strip()
            if not v:
                return False
            # Placeholder markers → definitely not a real constant
            if any(p in v for p in _PLACEHOLDER_PATTERNS):
                return False
            return True

        _bound = [
            (f["key"], f["value"]) for f in _default_config
            if f.get("bind") and _is_real_constant(str(f.get("value", "")))
        ]
        # Install fields = explicitly unbound OR bind:true but value is a placeholder
        _install_keys_seen: set = {k for k, _ in _bound}
        _install = [
            (f["key"], f.get("label", f["key"])) for f in _default_config
            if not f.get("bind") or f["key"] not in _install_keys_seen
        ]
        _lines: list = ["# Default Configuration — User Binding Decisions\n"]
        if _bound:
            _lines.append("## Hardcoded Constants (bind=True)\n"
                          "These MUST be class-level constants in connector.py. Do NOT add as install_fields.\n")
            for _key, _val in _bound:
                _lines.append(f"- **{_key}**: `{_val}`\n")
        if _install:
            _lines.append("\n## Install Fields (bind=False)\n"
                          "These MUST appear in connector.json install_fields. Do NOT hardcode.\n")
            for _key, _label in _install:
                _lines.append(f"- **{_key}** (label: \"{_label}\")\n")
        context["default_config_md"] = "".join(_lines)
    else:
        context["default_config_md"] = ""

    # ── Synthesize external docs + custom rules into KB (once, before steps) ──
    docs_urls: list = session.get("docs_urls") or []
    custom_rules_md: str = session.get("custom_rules_md") or ""
    if docs_urls or custom_rules_md:
        from integration.services.docs_synth_service import synthesize_and_ingest_docs

        def _docs_log(msg: str) -> None:
            pass  # logged inside the service; SSE events emitted below

        yield _sse_event("step_log", {
            "step_index": -1,
            "level": "info",
            "message": f"📚 Ingesting {len(docs_urls)} doc URL(s) + custom rules into knowledge base…",
        })
        try:
            synth_result = await synthesize_and_ingest_docs(
                docs_urls=docs_urls,
                custom_rules_md=custom_rules_md,
                tenant_id=tenant_id,
                provider=provider,
                service=service,
                log_cb=_docs_log,
            )
            ok_urls = synth_result.get("ingested_urls", [])
            errors = synth_result.get("errors", [])
            custom_ok = synth_result.get("custom_rules", False)
            summary = f"✅ Docs ingested: {len(ok_urls)} URL(s)"
            if custom_ok:
                summary += " + custom rules"
            if errors:
                summary += f" | ⚠ {len(errors)} error(s): {'; '.join(errors)}"
            yield _sse_event("step_log", {"step_index": -1, "level": "success", "message": summary})
        except Exception as _synth_err:
            logger.warning("execution.docs_synth_failed", error=str(_synth_err))
            yield _sse_event("step_log", {
                "step_index": -1, "level": "warn",
                "message": f"⚠ Doc ingestion failed (continuing): {_synth_err}",
            })

    # Mark session as executing.
    # force_restart: always take the lock (Re-Execute scenario).
    # Normal start: only proceed if status != executing, OR if heartbeat is stale (> 30s).
    if force_restart:
        # Unconditionally reset — clear execution_results too so the terminal starts clean
        await sessions_collection().update_one(
            {"_id": oid},
            {"$set": {
                "status": SessionStatus.EXECUTING.value,
                "service_slug": service_slug,
                "exec_heartbeat": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
                "execution_results": [],
            }},
        )
        logger.info("execution.force_restart", session_id=session_id)
    else:
        update_result = await sessions_collection().update_one(
            {"_id": oid, "status": {"$ne": SessionStatus.EXECUTING.value}},
            {"$set": {"status": SessionStatus.EXECUTING.value, "service_slug": service_slug,
                      "exec_heartbeat": datetime.utcnow(), "updated_at": datetime.utcnow()}},
        )
        if update_result.modified_count == 0:
            # Status is already "executing" — check if it's stale (crashed process left it stuck)
            live_doc = await sessions_collection().find_one({"_id": oid}, {"exec_heartbeat": 1})
            heartbeat = live_doc.get("exec_heartbeat") if live_doc else None
            stale = (not heartbeat) or ((datetime.utcnow() - heartbeat).total_seconds() > 30)
            if not stale:
                # A live execution is running — refuse to start a duplicate
                logger.warning("execution.rejected_duplicate", session_id=session_id)
                yield _sse_event("error", {"message": "Execution already in progress — wait for it to finish or use Stop button"})
                return
            # Stale lock — take over and reset
            await sessions_collection().update_one(
                {"_id": oid},
                {"$set": {"status": SessionStatus.EXECUTING.value, "service_slug": service_slug,
                          "exec_heartbeat": datetime.utcnow(), "updated_at": datetime.utcnow()}},
            )
            logger.warning("execution.force_reset_stale_executing", session_id=session_id)

    yield _sse_event("execution_start", {
        "session_id": session_id,
        "step_count": len(steps),
        "service": catalog["display_name"],
    })

    execution_results: List[Dict[str, Any]] = []
    all_passed = True

    # Heartbeat: update exec_heartbeat every 10s so stale-lock detection works correctly
    heartbeat_task = asyncio.ensure_future(_heartbeat_loop(oid))

    # Load existing execution results for skip logic
    existing_results = session.get("execution_results", [])

    # Load durable execution state from R2 (survives across sessions)
    exec_state = await r2_service.get_execution_state(provider, service_slug, tenant_id) or {}
    r2_completed: List[str] = exec_state.get("completed_steps", [])  # list of step_types
    exec_state_slug: str = exec_state.get("service_slug", "")

    # ── Slug mismatch: R2 state belongs to a DIFFERENT connector variant ──────────────────
    # This happens when a new connector (e.g. "shielva_gmail_connector") is built using a
    # cached plan from an old connector (e.g. "gmail_connector"). We must never skip steps
    # using the foreign connector's R2 state.
    if not force_restart and exec_state_slug and exec_state_slug != service_slug and r2_completed:
        source_dir = _output_dir(tenant_id, exec_state_slug)
        dest_dir   = _output_dir(tenant_id, service_slug)
        can_clone  = (
            source_dir.exists()
            and (source_dir / "connector.py").exists()
            and not dest_dir.exists()
        )
        if can_clone:
            # ── Clone mode ─────────────────────────────────────────────────────────────────
            # Derive the old connector display-name from the R2 state or the slug itself.
            old_connector_name = exec_state.get("connector_name") or exec_state_slug.replace("_", " ").title()
            new_connector_name = connector_name or service_slug.replace("_", " ").title()

            yield _sse_event("step_log", {
                "step_index": -1, "level": "info",
                "message": (
                    f"New connector identity detected — cloning '{exec_state_slug}' → '{service_slug}' "
                    f"and renaming all references from '{old_connector_name}' → '{new_connector_name}'…"
                ),
            })
            try:
                await _clone_connector_dir(
                    source_dir, dest_dir,
                    exec_state_slug, service_slug,
                    old_connector_name, new_connector_name,
                )
            except Exception as _clone_err:
                logger.error("execution.clone_failed", error=str(_clone_err))
                # Fall back to full fresh run
                r2_completed = []
                steps = [{**s, "status": StepStatus.PENDING.value} for s in steps]
                _rst: dict = {"updated_at": datetime.utcnow()}
                for _i in range(len(steps)):
                    _rst[f"plan.steps.{_i}.status"] = StepStatus.PENDING.value
                await sessions_collection().update_one({"_id": oid}, {"$set": _rst})
                yield _sse_event("step_log", {
                    "step_index": -1, "level": "warn",
                    "message": f"Clone failed ({_clone_err}) — running all steps fresh instead",
                })
            else:
                # Clone succeeded — mark all steps completed in MongoDB
                _done: dict = {
                    "updated_at": datetime.utcnow(),
                    "status": SessionStatus.COMPLETED.value,
                    "service_slug": service_slug,
                }
                for _i in range(len(steps)):
                    _done[f"plan.steps.{_i}.status"] = StepStatus.COMPLETED.value
                await sessions_collection().update_one({"_id": oid}, {"$set": _done})

                # Save R2 state for the new slug so future sessions pick up the right one
                _step_types = [s.get("type", "") for s in steps if s.get("type")]
                await r2_service.save_execution_state(
                    provider, service_slug, tenant_id, _step_types, session_id
                )

                # Emit step events so the UI shows all steps as completed
                for _i, _step in enumerate(steps):
                    yield _sse_event("step_start", {
                        "step_index": _i, "title": _step.get("title", f"Step {_i + 1}"),
                    })
                    yield _sse_event("step_complete", {
                        "step_index": _i, "status": "pass", "duration_ms": 0,
                        "output_preview": "Cloned and renamed from existing connector",
                    })

                yield _sse_event("execution_complete", {
                    "status": "completed",
                    "step_count": len(steps),
                    "passed": len(steps),
                    "failed": 0,
                    "message": f"Connector '{new_connector_name}' cloned and renamed successfully",
                })
                return
        else:
            # Source missing or dest already exists — cannot clone, run fresh
            r2_completed = []
            steps = [{**s, "status": StepStatus.PENDING.value} for s in steps]
            _rst2: dict = {"updated_at": datetime.utcnow()}
            for _i in range(len(steps)):
                _rst2[f"plan.steps.{_i}.status"] = StepStatus.PENDING.value
            await sessions_collection().update_one({"_id": oid}, {"$set": _rst2})
            yield _sse_event("step_log", {
                "step_index": -1, "level": "info",
                "message": "New connector — no existing directory to clone, running all steps fresh",
            })

    # force_restart: wipe ALL caches so every step runs from scratch
    if force_restart:
        r2_completed = []
        reset_update: dict = {"updated_at": datetime.utcnow()}
        for idx in range(len(steps)):
            reset_update[f"plan.steps.{idx}.status"] = StepStatus.PENDING.value
        await sessions_collection().update_one({"_id": oid}, {"$set": reset_update})
        steps = [{**s, "status": StepStatus.PENDING.value} for s in steps]
        # Clear R2 execution state so step-skip logic doesn't use old data
        try:
            await r2_service.save_execution_state(
                provider, service_slug, tenant_id, [], session_id
            )
        except Exception:
            pass

        # Wipe the generated code directory so Claude starts from a clean slate.
        # This removes stale files from previous runs (old flat layout, renamed files, etc.)
        # _output_dir already points to the package root, e.g. …/adsense_connector/
        out_dir_to_clean = _output_dir(tenant_id, service_slug)
        if out_dir_to_clean.exists():
            import shutil as _shutil
            try:
                _shutil.rmtree(str(out_dir_to_clean))
                logger.info("execution.force_restart_cleaned_dir", path=str(out_dir_to_clean))
                yield _sse_event("step_log", {
                    "step_index": -1, "level": "info",
                    "message": f"Re-Execute — removed old generated directory: {out_dir_to_clean.name}",
                })
            except Exception as clean_err:
                logger.warning("execution.force_restart_clean_failed", error=str(clean_err))

        # Clean up per-connector RAG vectors (stale knowledge from previous run)
        try:
            deleted = await knowledge_service.cleanup_connector_knowledge(
                tenant_id, provider, service,
            )
            if deleted:
                yield _sse_event("step_log", {
                    "step_index": -1, "level": "info",
                    "message": f"Re-Execute — cleaned {deleted} RAG knowledge records for this connector",
                })
        except Exception as rag_err:
            logger.warning("execution.rag_cleanup_failed", error=str(rag_err))

        # Re-ingest the plan into KB after RAG cleanup so Gemini has the spec during code gen
        try:
            from integration.services.knowledge_service import ingest_connector_docs
            from integration.services.r2_service import _build_plan_markdown
            from datetime import timezone
            _plan_data = session.get("plan") or {}
            _user_prompt = session.get("user_prompt", "")
            if _plan_data and _plan_data.get("steps"):
                _plan_md = _build_plan_markdown(
                    provider, service,
                    _user_prompt,
                    {
                        "steps": _plan_data["steps"],
                        "version": 1,
                        "package_structure": session.get("package_structure") or [],
                        "recommended_features": session.get("recommended_features") or [],
                    },
                    datetime.utcnow().isoformat(),
                )
                await ingest_connector_docs(
                    content=_plan_md,
                    title=f"Integration Plan: {provider} / {service}",
                    tenant_id=tenant_id,
                    provider=provider,
                    service=service,
                    session_id=session_id,
                )
                logger.info("execution.plan_reingested_to_kb", session_id=session_id)
        except Exception as kb_reingest_err:
            logger.warning("execution.plan_kb_reingest_failed", session_id=session_id, error=str(kb_reingest_err))

        yield _sse_event("step_log", {
            "step_index": -1, "level": "info",
            "message": "Re-Execute — running all steps fresh (cache cleared, directory wiped, RAG cleaned)",
        })

    # If connector.py doesn't exist (files deleted or never fully generated), bypass R2 cache
    # so all steps re-run. Check connector.py specifically — directory may exist from
    # _append_timeline's mkdir even if no real code was written yet.
    out_dir_check = _output_dir(tenant_id, service_slug)
    if not force_restart and r2_completed and not (out_dir_check / "connector.py").exists():
        logger.warning(
            "execution.connector_missing_clearing_r2",
            session_id=session_id, out_dir=str(out_dir_check),
        )
        r2_completed = []
        # Reset all step statuses in MongoDB to pending so they actually run
        reset_update = {"updated_at": datetime.utcnow()}
        for idx in range(len(steps)):
            reset_update[f"plan.steps.{idx}.status"] = StepStatus.PENDING.value
        await sessions_collection().update_one({"_id": oid}, {"$set": reset_update})
        # Also reset local steps list so the loop sees pending statuses
        steps = [{**s, "status": StepStatus.PENDING.value} for s in steps]
        yield _sse_event("step_log", {
            "step_index": -1, "level": "warn",
            "message": "Generated files not found on disk — ignoring R2 cache, running all steps fresh",
        })

    # When running from a specific step, force-clear R2/MongoDB status for that step
    # and all subsequent steps so they re-run even if previously completed.
    if from_step_index > 0:
        yield _sse_event("step_log", {
            "step_index": -1, "level": "info",
            "message": f"Running from step {from_step_index + 1} — re-executing selected step and onwards",
        })
        # Reset ALL steps to PENDING in MongoDB so the UI shows a clean slate.
        # Steps before from_step_index will still be skipped via r2_completed cache
        # and flip back to Done immediately — they just won't linger as stale "Done".
        force_reset = {"updated_at": datetime.utcnow()}
        for idx in range(len(steps)):
            force_reset[f"plan.steps.{idx}.status"] = StepStatus.PENDING.value
        await sessions_collection().update_one({"_id": oid}, {"$set": force_reset})
        # In-memory steps: all pending so the loop starts fresh visually
        steps = [{**s, "status": StepStatus.PENDING.value} for s in steps]
        # Remove ONLY the steps from from_step_index onwards from r2_completed
        # so earlier steps still get skipped (fast-path), but selected+ re-run
        r2_completed = [t for t in r2_completed if t not in {s.get("type") for s in steps[from_step_index:] if isinstance(s, dict)}]

    _rag_reingested_files: set = set()  # dedup: each file ingested at most once across all skipped steps

    for i, step in enumerate(steps):
        step_type = step.get("type", "")
        step_title = step.get("title", f"Step {i + 1}")
        step_config = step.get("config", {})

        # Skip if completed in this session (MongoDB) OR in a prior session (R2)
        # IMPORTANT: this must run BEFORE the write_tests break so that a completed
        # write_tests step is skipped rather than triggering the manual-required halt.
        step_current_status = step.get("status", "")
        if step_current_status == StepStatus.COMPLETED.value or step_type in r2_completed:
            existing = next(
                (r for r in existing_results if r.get("step_index") == i),
                None,
            )
            if existing:
                execution_results.append(existing)
            skip_reason = "Already completed — skipping" if step_current_status == StepStatus.COMPLETED.value else "Completed in prior session (R2) — skipping"
            # Persist completed status so session restore can rebuild logs correctly
            if step_current_status != StepStatus.COMPLETED.value:
                await sessions_collection().update_one(
                    {"_id": oid},
                    {"$set": {f"plan.steps.{i}.status": StepStatus.COMPLETED.value, "updated_at": datetime.utcnow()}},
                )
            yield _sse_event("step_skipped", {
                "step_index": i,
                "step_type": step_type,
                "title": step_title,
                "message": skip_reason,
            })
            logger.info("execution.step_skipped", session_id=session_id, step_index=i, step_type=step_type)

            # ── Re-ingest skipped step outputs into RAG ───────────────────
            # Deduplicated: each file is ingested at most once across ALL skipped steps.
            # Parallelised: all new files ingested concurrently via asyncio.gather.
            # Skip entirely if vectors already exist — re-ingestion is only needed on
            # cold starts (fresh backend, new device) not when the user is already on
            # the same page and RAG was populated during the initial execution run.
            try:
                _rag_already_populated = False
                if not _rag_reingested_files:  # only check on first skipped step
                    try:
                        _vc = await knowledge_service.get_connector_vector_count(
                            tenant_id=tenant_id, provider=provider, service=service
                        )
                        _rag_already_populated = (_vc.get("vector_count") or 0) > 0
                    except Exception:
                        pass

                if _rag_already_populated:
                    continue  # vectors exist — skip re-ingestion for all skipped steps

                _skip_out_dir = _output_dir(tenant_id, service_slug)
                _all_candidates = []
                for _fp in sorted(_skip_out_dir.rglob("*.py")):
                    if "__pycache__" not in _fp.parts:
                        _all_candidates.append(str(_fp.relative_to(_skip_out_dir)))
                for _extra in ["requirements.txt", "metadata/connector.json"]:
                    if (_skip_out_dir / _extra).exists():
                        _all_candidates.append(_extra)

                # Only ingest files not already ingested in a previous skipped step
                _new_files = [f for f in _all_candidates if f not in _rag_reingested_files]

                async def _ingest_one(rf):
                    rfp = _skip_out_dir / rf
                    if not rfp.exists():
                        return False
                    rfc = rfp.read_text(encoding="utf-8", errors="replace")
                    if not rfc.strip():
                        return False
                    await knowledge_service.ingest_step_output(
                        content=rfc, filename=rf,
                        tenant_id=tenant_id, provider=provider,
                        service=service, step_type=step_type,
                    )
                    return True

                if _new_files:
                    _results = await asyncio.gather(
                        *[_ingest_one(f) for f in _new_files], return_exceptions=True
                    )
                    _skip_rag_success = sum(1 for r in _results if r is True)
                    _skip_rag_failed = sum(1 for r in _results if r is not True)
                    _rag_reingested_files.update(_new_files)

                    _skip_rag_status = "success" if _skip_rag_failed == 0 else ("partial" if _skip_rag_success > 0 else "failed")
                    yield _sse_event("rag_ingest_complete", {
                        "step_index": i, "step_type": step_type,
                        "status": _skip_rag_status,
                        "ingested": _skip_rag_success,
                        "failed": _skip_rag_failed,
                        "total": len(_new_files),
                    })
                    logger.info("execution.skipped_step_reingested",
                        step_type=step_type, files=_new_files, ingested=_skip_rag_success)
            except Exception as _skip_rag_err:
                logger.warning("execution.skipped_step_reingest_failed",
                    step_type=step_type, error=str(_skip_rag_err))

            continue

        # skip_llm mode: Claude CLI already handled LLM steps locally.
        # Mark them completed in MongoDB and skip without running the LLM executor.
        # EXCEPTION: for write_connector, verify connector.py actually exists on disk.
        # If Claude ran but didn't write the file (e.g. ran out of context, wrote wrong
        # files), fall through to the normal LLM executor so the backend writes it instead.
        if skip_llm and step_type in _LLM_STEP_TYPES:
            _should_skip = True
            if step_type == "write_connector":
                _connector_py = _output_dir(tenant_id, service_slug) / "connector.py"
                if not _connector_py.exists():
                    _should_skip = False
                    logger.warning(
                        "execution.skip_llm_connector_missing",
                        session_id=session_id, step_index=i,
                        output_dir=str(_output_dir(tenant_id, service_slug)),
                    )
                    yield _sse_event("step_log", {
                        "step_index": i,
                        "level": "warn",
                        "message": "connector.py not found — Claude may not have written it. Running backend LLM write_connector instead.",
                    })
            if _should_skip:
                await sessions_collection().update_one(
                    {"_id": oid},
                    {"$set": {f"plan.steps.{i}.status": StepStatus.COMPLETED.value, "updated_at": datetime.utcnow()}},
                )
                yield _sse_event("step_skipped", {
                    "step_index": i,
                    "step_type": step_type,
                    "title": step_title,
                    "message": "LLM step handled by Claude CLI — skipping",
                })
                logger.info("execution.step_skipped_llm", session_id=session_id, step_index=i, step_type=step_type)
                continue
            # Fall through to normal LLM executor for write_connector when file is missing

        # write_tests requires manual user action — STOP auto-run here, notify UI.
        # Only reached when the step is NOT already completed (checked above).
        if step_type == "write_tests":
            # If the user already generated tests manually (test file exists on disk),
            # auto-mark the step as completed and continue — don't interrupt execution.
            _test_file = _output_dir(tenant_id, service_slug) / "tests" / "test_connector.py"
            if _test_file.exists() and _test_file.stat().st_size > 0:
                await sessions_collection().update_one(
                    {"_id": oid},
                    {"$set": {f"plan.steps.{i}.status": StepStatus.COMPLETED.value, "updated_at": datetime.utcnow()}},
                )
                yield _sse_event("step_skipped", {
                    "step_index": i,
                    "step_type": step_type,
                    "title": step_title,
                    "message": "Test file already generated — skipping",
                })
                logger.info("execution.write_tests_auto_skipped", session_id=session_id, step_index=i)
                continue
            yield _sse_event("step_manual_required", {
                "step_index": i,
                "step_type": step_type,
                "title": step_title,
                "message": "Write Unit Tests requires manual execution — select methods and click Generate Test Cases in the accordion.",
            })
            logger.info("execution.step_manual_required", session_id=session_id, step_index=i, step_type=step_type)
            break  # stop auto-run — run_tests cannot proceed without the test file

        # version_upgrade requires user input — pause execution until user selects version
        if step_type == "version_upgrade":
            # Use the last confirmed version from MongoDB as the true "current" version.
            # The regenerated connector.json always starts at "1.0.0", so reading only
            # from the file would lose track of previous bumps (e.g. 1.0.1 → shows 1.0.0).
            db_version = session.get("metadata_version") or ""
            meta_path = _output_dir(tenant_id, service_slug) / "metadata" / "connector.json"
            file_version = "1.0.0"
            try:
                if meta_path.exists():
                    _meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
                    file_version = str(_meta_data.get("version", "1.0.0"))
            except Exception:
                pass
            # Prefer the DB version (reflects previous bumps); fall back to file version
            current_version = db_version if db_version else file_version

            suggestions = _compute_version_suggestions(current_version)

            await sessions_collection().update_one(
                {"_id": oid},
                {"$set": {
                    f"plan.steps.{i}.status": StepStatus.PENDING_VERSION.value,
                    "version_upgrade_pending": {
                        "current_version": current_version,
                        "suggestions": suggestions,
                        "step_index": i,
                    },
                }},
            )

            yield _sse_event("version_upgrade_required", {
                "step_index": i,
                "current_version": current_version,
                "suggestions": suggestions,
                "message": "Select a version number to release this connector.",
            })

            logger.info("execution.version_upgrade_pending",
                        session_id=session_id, step_index=i, current_version=current_version)
            break  # pause — resumed by select-version API endpoint

        yield _sse_event("step_start", {
            "step_index": i,
            "step_type": step_type,
            "title": step_title,
        })

        # Update step status to executing
        await sessions_collection().update_one(
            {"_id": oid},
            {"$set": {f"plan.steps.{i}.status": StepStatus.EXECUTING.value}},
        )

        # ── Inject failure context from R2/Redis ─────────────────────────────
        # Always try — get_failure_context returns None if no failure exists.
        # This covers: Retry button, Run from this step, auto-run on a previously-failed step.
        # The handler receives "error_details" with full prior failure history so the LLM
        # doesn't repeat strategies that already failed.
        context.pop("error_details", None)
        context.pop("failure_id", None)
        try:
            _prior_failure = await failure_tracker.get_failure_context(
                session_id=session_id,
                step_index=i,
                provider=provider,
                service=service,
                tenant_id=tenant_id,
            )
            if _prior_failure:
                context["error_details"] = failure_tracker.build_failure_context_for_llm(_prior_failure)
                context["failure_id"] = _prior_failure["failure_id"]
                yield _sse_event("step_log", {
                    "step_index": i,
                    "level": "info",
                    "message": f"📋 Loaded prior failure history ({_prior_failure['failure_id']}) — context injected",
                })
        except Exception as _ft_exc:
            logger.warning("execution.failure_context_load_error", step_index=i, error=str(_ft_exc))

        # Inject step_type into context so _inject_rag_context can route queries per step
        context["step_type"] = step_type

        handler = STEP_HANDLERS.get(step_type)
        if not handler:
            result = {"status": "fail", "output": f"Unknown step type: {step_type}"}
            yield _sse_event("step_log", {"step_index": i, "level": "error", "message": f"Unknown step type: {step_type}"})
        else:
            started = time.time()

            # Create log callback that pushes to a queue for real-time streaming
            log_messages: List[Dict[str, str]] = []
            log_queue: asyncio.Queue[Dict[str, str]] = asyncio.Queue()

            async def log_cb(level: str, msg: str, _idx=i):
                entry = {"level": level, "message": msg}
                log_messages.append(entry)
                await log_queue.put(entry)
                log_fn = logger.error if level == "error" else (logger.warning if level == "warning" else logger.info)
                log_fn("execution.step_log", session_id=session_id, step_index=_idx, step_type=step_type, message=msg)

            # Run handler + drain log queue concurrently for real-time streaming
            handler_done = asyncio.Event()
            handler_result_holder: List[Dict] = []

            async def _run_handler():
                try:
                    r = await handler(step_config, context, log_cb)
                    handler_result_holder.append(r)
                except Exception as exc:
                    handler_result_holder.append({"status": "fail", "output": str(exc)})
                    log_messages.append({"level": "error", "message": f"Exception: {exc}"})
                finally:
                    handler_done.set()

            handler_task = asyncio.create_task(_run_handler())

            # Stream logs in real-time as the handler produces them.
            # Yield ": keepalive" SSE comments every ~500ms when no log arrives
            # so HTTP proxies don't buffer the stream.
            while not handler_done.is_set() or not log_queue.empty():
                try:
                    entry = await asyncio.wait_for(log_queue.get(), timeout=0.5)
                    yield _sse_event("step_log", {
                            "step_index": i,
                            "level": entry["level"],
                            "message": entry["message"],
                        })
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # flush proxy buffers

            # Drain any remaining log entries
            while not log_queue.empty():
                entry = await log_queue.get()
                yield _sse_event("step_log", {
                        "step_index": i,
                        "level": entry["level"],
                        "message": entry["message"],
                    })

            await handler_task  # ensure task completes
            result = handler_result_holder[0] if handler_result_holder else {"status": "fail", "output": "Handler returned no result"}
            duration_ms = round((time.time() - started) * 1000, 1)

        step_status = result.get("status", "fail")
        if step_status != "pass":
            all_passed = False

        # Persist execution result with logs
        step_duration = duration_ms if "duration_ms" in locals() else 0
        step_log_messages = log_messages if "log_messages" in locals() else []
        exec_result = StepExecutionResult(
            step_index=i,
            status=step_status,
            output=json.dumps(result.get("output", ""))[:5000],
            duration_ms=step_duration,
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
        result_dict = exec_result.model_dump(mode="json")
        # Attach step logs (capped to last 100 lines to avoid memory bloat)
        result_dict["logs"] = step_log_messages[-100:]
        execution_results.append(result_dict)

        logger.info(
            "execution.step_finished",
            session_id=session_id,
            step_index=i,
            step_type=step_type,
            step_status=step_status,
            duration_ms=step_duration,
            log_count=len(step_log_messages),
        )

        # Update step status in DB + persist result immediately
        final_step_status = StepStatus.COMPLETED.value if step_status == "pass" else StepStatus.FAILED.value
        slim_result = await _offload_exec_result_to_r2(
            provider=provider, service_slug=service_slug, session_id=session_id, result_dict=result_dict,
        )
        await sessions_collection().update_one(
            {"_id": oid},
            {
                "$set": {f"plan.steps.{i}.status": final_step_status, "updated_at": datetime.utcnow()},
                "$push": {"execution_results": slim_result},
            },
        )

        # ── Failure tracking ──────────────────────────────────────────────────
        if step_status == "pass":
            # Clear any existing failure for this step
            asyncio.ensure_future(failure_tracker.resolve_failure(
                session_id=session_id,
                step_index=i,
                provider=provider,
                service=service,
                tenant_id=tenant_id,
            ))
        else:
            # Record new failure to R2 + Redis — but ONLY for real code/logic failures,
            # not LLM infrastructure errors (rate limits, timeouts, empty responses).
            error_summary = "; ".join(
                e["message"] for e in step_log_messages if e.get("level") in ("error", "warn")
            )[:500] or str(result.get("output", ""))[:500]
            _llm_infra_error = any(
                kw in error_summary.lower()
                for kw in ("429", "503", "402", "rate limit", "too many requests",
                           "service unavailable", "invalid response (not python",
                           "empty response", "timed out", "falling back")
            )
            if not _llm_infra_error:
                full_output = str(result.get("output", ""))
                asyncio.ensure_future(failure_tracker.create_failure(
                    session_id=session_id,
                    step_index=i,
                    step_type=step_type,
                    provider=provider,
                    service=service,
                    tenant_id=tenant_id,
                    error_summary=error_summary,
                    full_output=full_output,
                ))

        # ── Update running step_memory so every subsequent handler knows what happened ─────
        # This is the "conversation memory" — each step reads what all prior steps built/failed.
        _mem = context["step_memory"]
        if step_status == "pass":
            if step_type not in _mem["completed_steps"]:
                _mem["completed_steps"].append(step_type)

            out_dir_mem = _output_dir(tenant_id, service_slug)

            if step_type == "install_deps":
                # Record which packages were actually installed
                _req = out_dir_mem / "requirements.txt"
                if _req.exists():
                    _mem["installed_packages"] = [
                        l.strip() for l in _req.read_text().splitlines() if l.strip() and not l.startswith("#")
                    ]

            elif step_type == "write_connector":
                # Record class name + public methods so test/fix handlers know the exact API surface
                _cp = out_dir_mem / "connector.py"
                if _cp.exists():
                    try:
                        import ast as _ast_mem
                        _ct = _ast_mem.parse(_cp.read_text(encoding="utf-8"))
                        for _cn in _ast_mem.walk(_ct):
                            if isinstance(_cn, _ast_mem.ClassDef) and "Connector" in _cn.name:
                                _mem["connector_class_name"] = _cn.name
                                context["class_name"] = _cn.name  # live-update shared context too
                                _mem["connector_methods"] = [
                                    m.name for m in _cn.body
                                    if isinstance(m, (_ast_mem.FunctionDef, _ast_mem.AsyncFunctionDef))
                                    and not m.name.startswith("_")
                                ]
                                break
                    except Exception:
                        pass

            elif step_type == "write_tests":
                # Record which methods have test coverage
                _tests_dir_mem = out_dir_mem / "tests"
                if _tests_dir_mem.exists():
                    _covered = []
                    for _tf in _tests_dir_mem.glob("test_*.py"):
                        try:
                            import ast as _ast_t
                            _tt = _ast_t.parse(_tf.read_text(encoding="utf-8"))
                            for _fn in _ast_t.walk(_tt):
                                if isinstance(_fn, (_ast_t.FunctionDef, _ast_t.AsyncFunctionDef)) and _fn.name.startswith("test_"):
                                    _covered.append(_fn.name)
                        except Exception:
                            pass
                    _mem["test_methods_covered"] = _covered

            elif step_type == "run_tests":
                # Record test pass/fail counts for the fix handler
                _out_run = result.get("output", {})
                if isinstance(_out_run, dict):
                    _mem["last_test_passed"] = _out_run.get("passed", 0)
                    _mem["last_test_failed"] = _out_run.get("failed", 0)

        else:
            # Step failed — record it so subsequent handlers know what went wrong
            _err_summary = error_summary if "error_summary" in dir() else str(result.get("output", ""))[:300]
            _mem["errors_encountered"].append(f"[{step_type}] {_err_summary}")
            _mem["fix_attempts"][step_type] = _mem["fix_attempts"].get(step_type, 0) + 1

        # Persist durable execution state to R2 after each pass (survives new sessions)
        if step_status == "pass":
            if step_type not in r2_completed:
                r2_completed = r2_completed + [step_type]
            await r2_service.save_execution_state(provider, service_slug, tenant_id, r2_completed, session_id)
            out_dir = _output_dir(tenant_id, service_slug)
            _append_timeline(out_dir, session_id, step_title, i, step_status, step_duration)

            # ── Per-connector RAG ingestion ────────────────────────────
            # Ingest ALL generated files after every successful step so the
            # LLM always has full context for subsequent steps and fix cycles.
            # Scans the entire output directory — no step-type-specific lists.
            try:
                _rag_files = []
                # All Python files (excludes __pycache__ only — tests ARE included)
                for _fp in sorted(out_dir.rglob("*.py")):
                    if "__pycache__" not in _fp.parts:
                        _rag_files.append(str(_fp.relative_to(out_dir)))
                # Non-Python assets
                for _extra in ["requirements.txt", "metadata/connector.json"]:
                    _ep = out_dir / _extra
                    if _ep.exists() and _extra not in _rag_files:
                        _rag_files.append(_extra)

                if _rag_files:
                    yield _sse_event("rag_ingest_start", {
                        "step_index": i,
                        "step_type": step_type,
                        "file_count": len(_rag_files),
                    })

                _rag_success = 0
                _rag_failed = 0
                for _rf in _rag_files:
                    _rfp = out_dir / _rf
                    if _rfp.exists():
                        _rfc = _rfp.read_text(encoding="utf-8", errors="replace")
                        if _rfc.strip():
                            try:
                                await knowledge_service.ingest_step_output(
                                    content=_rfc,
                                    filename=_rf,
                                    tenant_id=tenant_id,
                                    provider=provider,
                                    service=service,
                                    step_type=step_type,
                                )
                                _rag_success += 1
                            except Exception:
                                _rag_failed += 1

                if _rag_files:
                    _rag_status = "success" if _rag_failed == 0 else ("partial" if _rag_success > 0 else "failed")
                    yield _sse_event("rag_ingest_complete", {
                        "step_index": i,
                        "step_type": step_type,
                        "status": _rag_status,
                        "ingested": _rag_success,
                        "failed": _rag_failed,
                        "total": len(_rag_files),
                    })
            except Exception as _rag_err:
                logger.warning("execution.rag_ingest_step_failed", step=step_type, error=str(_rag_err))
                yield _sse_event("rag_ingest_complete", {
                    "step_index": i,
                    "step_type": step_type,
                    "status": "failed",
                    "ingested": 0,
                    "failed": 0,
                    "total": 0,
                    "error": str(_rag_err),
                })

        yield _sse_event("step_complete", {
            "step_index": i,
            "status": step_status,
            "duration_ms": step_duration,
            "output_preview": str(result.get("output", ""))[:500],
        })

        # Stop on critical failure (except install_deps partial is ok)
        if step_status == "fail" and step_type not in ("install_deps", "run_tests"):
            yield _sse_event("execution_error", {
                "step_index": i,
                "message": f"Step '{step_title}' failed — stopping execution",
            })
            break

    # Analyze generated code quality
    out_dir = _output_dir(tenant_id, service_slug)
    quality = analyze_directory(str(out_dir))

    # Collect generated files info
    generated_files = []
    if out_dir.exists():
        for py_file in out_dir.rglob("*.py"):
            rel = str(py_file.relative_to(out_dir))
            generated_files.append(GeneratedFile(
                path=rel,
                size=py_file.stat().st_size,
                language="python",
                quality_score=next(
                    (f["quality_score"] for f in quality.get("files", []) if f.get("path") == rel),
                    None,
                ),
            ).model_dump())

    # Update session with final status + generated files (execution_results already persisted per-step)
    final_status = SessionStatus.COMPLETED.value if all_passed else SessionStatus.FAILED.value
    final_set: Dict[str, Any] = {
        "status": final_status,
        "generated_files": generated_files,
        "updated_at": datetime.utcnow(),
    }
    # Read the LLM-generated connector name from connector.json and store it in a separate
    # field so the user's original connector_name is never overwritten.
    try:
        connector_json_path = _output_dir(tenant_id, service_slug) / "metadata" / "connector.json"
        if connector_json_path.exists():
            _meta = json.loads(connector_json_path.read_text())
            _generated_name = _meta.get("name") or _meta.get("display_name")
            if _generated_name:
                final_set["generated_connector_name"] = _generated_name
    except Exception:
        pass
    await sessions_collection().update_one(
        {"_id": oid},
        {"$set": final_set},
    )

    logger.info(
        "execution.complete",
        session_id=session_id,
        tenant_id=tenant_id,
        status="completed" if all_passed else "failed",
        file_count=len(generated_files),
        step_count=len(steps),
        avg_quality=quality.get("average_quality_score", 0),
    )

    # Cancel heartbeat — execution is done
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass

    yield _sse_event("execution_complete", {
        "session_id": session_id,
        "status": "completed" if all_passed else "failed",
        "file_count": len(generated_files),
        "average_quality_score": quality.get("average_quality_score", 0),
    })


# ── Non-streaming execution ──────────────────────────────────────────

async def execute_plan_sync(session_id: str, tenant_id: Optional[str]) -> Dict[str, Any]:
    """Execute plan without SSE streaming. Returns final result dict."""
    events = []
    async for event_str in execute_plan(session_id, tenant_id):
        # Parse SSE string
        for line in event_str.strip().split("\n"):
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass

    # Return the last execution_complete event or error
    for ev in reversed(events):
        if "status" in ev or "message" in ev:
            return ev

    return {"status": "unknown", "events": events}


# ── RAG ingestion helper shared by retry / fix / test-generate paths ──

async def _ingest_files_for_step(
    out_dir: "Path",
    step_type: str,
    tenant_id: str,
    provider: str,
    service: str,
) -> tuple[int, int]:
    """Ingest generated files for *step_type* into the connector KB.

    Mirrors the RAG ingestion logic in the main execution loop so that
    retry, attempt-fix, and manual test-generate paths also index output.
    Returns (ingested_count, failed_count).
    """
    from pathlib import Path as _Path

    _rag_files: list[str] = []
    if step_type in ("write_connector", "scaffold_code"):
        _rag_files = ["connector.py", "config.py", "__init__.py", "exceptions.py"]
        for _subdir in ["helpers", "client"]:
            _sd = out_dir / _subdir
            if _sd.exists():
                for _sf in _sd.glob("*.py"):
                    _rag_files.append(f"{_subdir}/{_sf.name}")
    elif step_type == "write_tests":
        _tests_d = out_dir / "tests"
        if _tests_d.exists():
            for _tf in _tests_d.glob("*.py"):
                _rag_files.append(f"tests/{_tf.name}")
    elif step_type == "install_deps":
        _rag_files = ["requirements.txt"]
    elif step_type == "generate_metadata":
        _rag_files = ["metadata/connector.json"]

    ingested, failed = 0, 0
    for _rf in _rag_files:
        _rfp = out_dir / _rf
        if not _rfp.exists():
            continue
        try:
            _rfc = _rfp.read_text(encoding="utf-8", errors="replace")
            if _rfc.strip():
                await knowledge_service.ingest_step_output(
                    content=_rfc,
                    filename=_rf,
                    tenant_id=tenant_id,
                    provider=provider,
                    service=service,
                    step_type=step_type,
                )
                ingested += 1
        except Exception:
            failed += 1
    return ingested, failed


# ── Single-step execution (for retry) ──────────────────────────────

async def execute_single_step(
    session_id: str,
    tenant_id: str,
    step_index: int,
) -> Dict[str, Any]:
    """Execute (or re-execute) a single step within an existing session.

    When the step was previously failed, loads the failure context from R2/Redis
    and injects it into the handler context so the step can be re-run with full
    awareness of what went wrong before.

    Resets the step status, removes old result, runs the handler, persists new result.
    Returns the step execution result dict.
    """
    oid = ObjectId(session_id)
    # Use _id only — gateway overwrites tenant_id from JWT which may differ from stored session
    session = await sessions_collection().find_one({"_id": oid})
    if not session:
        raise ValueError(f"Session {session_id} not found")

    # Resolve real tenant from stored doc
    tenant_id = session.get("tenant_id") or tenant_id

    # Phase 4: rehydrate the full plan body before reading step config.
    await _hydrate_plan_from_r2(session)
    plan = session.get("plan", {})
    steps = plan.get("steps", [])
    if step_index < 0 or step_index >= len(steps):
        raise ValueError(f"Step index {step_index} out of range (0..{len(steps) - 1})")

    step = steps[step_index]
    if not isinstance(step, dict):
        raise ValueError(
            f"Step at index {step_index} is not a dict (got {type(step).__name__}: {str(step)[:80]}). "
            f"Session plan may be corrupted — re-generate the plan."
        )
    step_type = step.get("type", "")
    step_title = step.get("title", f"Step {step_index + 1}")
    step_config = step.get("config", {})

    # Note: completed steps CAN be re-run (regenerate). Downstream steps are reset below.

    handler = STEP_HANDLERS.get(step_type)
    if not handler:
        raise ValueError(f"Unknown step type: {step_type}")

    provider = session["provider"]
    service = session["service"]
    catalog = get_service_detail(provider, service)
    if not catalog:
        _cn = session.get("connector_name") or f"{provider.title()} {service.replace('_', ' ').title()}"
        catalog = {
            "provider": provider, "service": service, "service_key": service,
            "display_name": _cn,
            "description": session.get("user_prompt") or f"Custom connector for {_cn}",
            "auth_type": session.get("auth_type") or "api_key",
            "category": "custom",
        }

    _connector_name = session.get("connector_name", "")
    service_slug = (
        _slug_from_connector_name(_connector_name)
        if _connector_name
        else _service_slug(provider, service)
    )
    class_name = _to_class_name(service)

    # ── Load prior failure context (R2 → Redis) for failed steps ─────────
    prior_failure = await failure_tracker.get_failure_context(
        session_id=session_id,
        step_index=step_index,
        provider=provider,
        service=service,
        tenant_id=tenant_id,
    )
    failure_context_str = failure_tracker.build_failure_context_for_llm(prior_failure)

    # Build error_details from previous execution result + failure history
    existing_results = session.get("execution_results", [])
    prev_result = next((r for r in existing_results if r.get("step_index") == step_index), None)
    error_details = ""
    if prev_result:
        parts = []
        raw_out = prev_result.get("output", "")
        if raw_out:
            parts.append(f"Output: {raw_out}")
        for log_entry in prev_result.get("logs", []):
            if log_entry.get("level") in ("error", "warn"):
                parts.append(f"{log_entry['level'].upper()}: {log_entry['message']}")
        error_details = "\n".join(parts)
    if failure_context_str:
        error_details = f"{failure_context_str}\n\nCurrent Error:\n{error_details}" if error_details else failure_context_str

    context = {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "provider": provider,
        "service": service,
        "service_slug": service_slug,
        "service_name": catalog["display_name"],
        "class_name": class_name,
        "auth_type": catalog.get("auth_type", "unknown"),
        "sdk_package": catalog.get("sdk_package", ""),
        "docs_url": catalog.get("docs_url", ""),
        "default_scopes": catalog.get("default_scopes", []),
        "user_prompt": session.get("user_prompt", ""),
        "run_kind": session.get("run_kind", "build"),
        "parent_session_id": session.get("parent_session_id", ""),
        "is_enhance": session.get("run_kind") == "enhance",
        "package_structure": _build_package_structure(plan),
        "error_details": error_details,
        "failure_id": prior_failure.get("failure_id") if prior_failure else None,
    }

    # Reset this step + undo all downstream steps so stale results are cleared.
    # Any step after step_index is reverted to PENDING — their outputs are now
    # potentially invalid because this step's output has changed.
    downstream_indices = list(range(step_index + 1, len(steps)))
    _status_updates: dict = {f"plan.steps.{step_index}.status": StepStatus.EXECUTING.value}
    for _di in downstream_indices:
        _status_updates[f"plan.steps.{_di}.status"] = StepStatus.PENDING.value
    await sessions_collection().update_one(
        {"_id": oid},
        {
            "$set": _status_updates,
            # Pull results for this step AND all downstream steps
            "$pull": {"execution_results": {"step_index": {"$gte": step_index}}},
        },
    )
    if downstream_indices:
        logger.info(
            "codegen.regenerate_step_reset_downstream",
            session_id=session_id,
            regenerated_step=step_index,
            reset_steps=downstream_indices,
        )

    # Also trim R2 execution state — remove completed_steps for this step and all downstream.
    # Without this, execute_plan would skip the now-invalidated downstream steps on re-run.
    try:
        _invalidated_types = {steps[i].get("type") for i in [step_index] + downstream_indices if isinstance(steps[i], dict)}
        _exec_state = await r2_service.get_execution_state(provider, service, tenant_id) or {}
        _r2_done: list = _exec_state.get("completed_steps", [])
        _r2_done_trimmed = [t for t in _r2_done if t not in _invalidated_types]
        if len(_r2_done_trimmed) < len(_r2_done):
            await r2_service.save_execution_state(provider, service_slug, tenant_id, _r2_done_trimmed, session_id)
            logger.info(
                "codegen.regenerate_step_r2_trimmed",
                session_id=session_id,
                removed_types=list(_invalidated_types),
            )
    except Exception as _r2_err:
        logger.warning("codegen.regenerate_step_r2_trim_failed", error=str(_r2_err))

    log_messages: List[Dict[str, str]] = []

    async def log_cb(level: str, msg: str):
        log_messages.append({"level": level, "message": msg})

    # ── Auto-fix retry for smoke_test (and run_tests) on single-step execution ──
    # execute_single_step is used by "Run from step N" — it must mirror the retry
    # logic in execute_plan_steps so smoke test fix is applied even outside a full run.
    # Exception: if connector.py doesn't exist, skip retries entirely — auto-fix
    # cannot help when there is no file to fix.
    _connector_py = _output_dir(tenant_id, service_slug) / "connector.py"
    if step_type == "smoke_test" and not _connector_py.exists():
        log_messages.append({"level": "error", "message": "connector.py not found — run write_connector (Step 3) first"})
        result = {"status": "fail", "output": "connector.py missing"}
        final_step_status = StepStatus.FAILED.value
        exec_result = StepExecutionResult(
            step_index=step_index,
            status="fail",
            output=json.dumps("connector.py missing"),
            duration_ms=0,
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
        result_dict = exec_result.model_dump(mode="json")
        result_dict["logs"] = log_messages
        slim_result = await _offload_exec_result_to_r2(
            provider=provider, service_slug=service_slug, session_id=session_id, result_dict=result_dict,
        )
        await sessions_collection().update_one(
            {"_id": oid},
            {"$set": {f"plan.steps.{step_index}.status": final_step_status, "updated_at": datetime.utcnow()},
             "$push": {"execution_results": slim_result}},
        )
        logger.info("execution.single_step_finished", session_id=session_id, step_index=step_index,
                    step_type=step_type, status="fail", duration_ms=0)
        return result_dict

    # ── Guard: implementation_plan.md must exist for write_connector ────────────
    # write_connector genuinely needs the implementation plan to generate code.
    # install_deps does NOT need it — handle_install_deps falls back gracefully to
    # config.packages → requirements.txt when the plan file is absent.
    if step_type == "write_connector":
        _impl_plan_path = _output_dir(tenant_id, service_slug) / "implementation_plan.md"
        # Only enforce when a generate_implementation_plan step exists in the plan.
        _has_impl_step = any(
            isinstance(s, dict) and s.get("type") == "generate_implementation_plan"
            for s in steps
        )
        if _has_impl_step and not _impl_plan_path.exists():
            _impl_step_num = next(
                (s.get("index", i) + 1 for i, s in enumerate(steps)
                 if isinstance(s, dict) and s.get("type") == "generate_implementation_plan"),
                1,
            )
            _err_msg = (
                f"implementation_plan.md not found — run Step {_impl_step_num} "
                f"(generate_implementation_plan) before running write_connector"
            )
            log_messages.append({"level": "error", "message": _err_msg})
            result = {"status": "fail", "output": _err_msg}
            exec_result = StepExecutionResult(
                step_index=step_index, status="fail",
                output=json.dumps(_err_msg), duration_ms=0,
                started_at=datetime.utcnow(), finished_at=datetime.utcnow(),
            )
            result_dict = exec_result.model_dump(mode="json")
            result_dict["logs"] = log_messages
            slim_result = await _offload_exec_result_to_r2(
                provider=provider, service_slug=service_slug, session_id=session_id, result_dict=result_dict,
            )
            await sessions_collection().update_one(
                {"_id": oid},
                {"$set": {f"plan.steps.{step_index}.status": StepStatus.FAILED.value, "updated_at": datetime.utcnow()},
                 "$push": {"execution_results": slim_result}},
            )
            logger.info("execution.single_step_finished", session_id=session_id, step_index=step_index,
                        step_type=step_type, status="fail", duration_ms=0)
            return result_dict

    _single_max_attempts = 3 if step_type == "smoke_test" else 2 if step_type == "run_tests" else 1

    started = time.time()
    result = {"status": "fail", "output": ""}
    for _attempt in range(_single_max_attempts):
        if _attempt > 0:
            # Apply fix before retrying
            _fix_handler = FIX_HANDLERS.get(step_type)
            if _fix_handler:
                log_messages.append({"level": "info", "message": f"Auto-fix attempt {_attempt}/{_single_max_attempts - 1} for {step_title}..."})
                try:
                    _err_ctx = str(result.get("output", ""))
                    _fix_result = await _fix_handler(step_config, {**context, "error_details": _err_ctx}, log_cb)
                    # For smoke_test: fix handler already verifies — if it passes, use that result
                    if step_type == "smoke_test" and _fix_result.get("status") == "pass":
                        result = _fix_result
                        break
                except Exception as _fe:
                    log_messages.append({"level": "error", "message": f"Fix attempt {_attempt} exception: {_fe}"})

        try:
            result = await handler(step_config, context, log_cb)
        except Exception as exc:
            result = {"status": "fail", "output": str(exc)}
            log_messages.append({"level": "error", "message": f"Exception: {exc}"})

        if result.get("status") == "pass":
            break

        if _attempt < _single_max_attempts - 1:
            log_messages.append({"level": "warn", "message": f"Step failed — will attempt auto-fix (attempt {_attempt + 1}/{_single_max_attempts - 1})..."})

    duration_ms = round((time.time() - started) * 1000, 1)
    step_status = result.get("status", "fail")

    # ── Failure tracking: resolve on pass, create/update on fail ─────────
    if step_status == "pass":
        asyncio.ensure_future(failure_tracker.resolve_failure(
            session_id=session_id,
            step_index=step_index,
            provider=provider,
            service=service,
            tenant_id=tenant_id,
        ))
    else:
        error_summary = "; ".join(
            e["message"] for e in log_messages if e.get("level") in ("error", "warn")
        )[:500] or str(result.get("output", ""))[:500]
        _llm_infra_error = any(
            kw in error_summary.lower()
            for kw in ("429", "503", "402", "rate limit", "too many requests",
                       "service unavailable", "invalid response (not python",
                       "empty response", "timed out", "falling back")
        )
        if not _llm_infra_error:
          if prior_failure:
            # Append retry attempt to existing failure doc
            asyncio.ensure_future(failure_tracker.append_fix_attempt(
                failure_id=prior_failure["failure_id"],
                provider=provider,
                service=service,
                tenant_id=tenant_id,
                outcome="failed",
                strategy=f"Manual retry of {step_type}",
                details=str(result.get("output", "")) + "\n" + error_summary,
            ))
          else:
            # No prior failure doc — create a fresh one
            asyncio.ensure_future(failure_tracker.create_failure(
                session_id=session_id,
                step_index=step_index,
                step_type=step_type,
                provider=provider,
                service=service,
                tenant_id=tenant_id,
                error_summary=error_summary,
                full_output=str(result.get("output", "")),
            ))

    exec_result = StepExecutionResult(
        step_index=step_index,
        status=step_status,
        output=json.dumps(result.get("output", ""))[:5000],
        duration_ms=duration_ms,
        started_at=datetime.utcnow(),
        finished_at=datetime.utcnow(),
    )
    result_dict = exec_result.model_dump(mode="json")
    result_dict["logs"] = log_messages[-100:]

    # Persist result + update step status
    final_step_status = StepStatus.COMPLETED.value if step_status == "pass" else StepStatus.FAILED.value
    slim_result = await _offload_exec_result_to_r2(
        provider=provider, service_slug=service_slug, session_id=session_id, result_dict=result_dict,
    )
    await sessions_collection().update_one(
        {"_id": oid},
        {
            "$set": {
                f"plan.steps.{step_index}.status": final_step_status,
                "updated_at": datetime.utcnow(),
            },
            "$push": {"execution_results": slim_result},
        },
    )

    # ── RAG indexing for retry ────────────────────────────────────────
    # Index the (re-)generated files so subsequent fix attempts and
    # the LLM have up-to-date knowledge of the current connector state.
    if step_status == "pass" and step_type in (
        "write_connector", "scaffold_code", "write_tests",
        "install_deps", "generate_metadata",
    ):
        _ingest_out_dir = _output_dir(tenant_id, service_slug)
        asyncio.ensure_future(_ingest_files_for_step(
            _ingest_out_dir, step_type, tenant_id, provider, service
        ))

    logger.info(
        "execution.single_step_finished",
        session_id=session_id,
        step_index=step_index,
        step_type=step_type,
        status=step_status,
        duration_ms=duration_ms,
        had_prior_failure=bool(prior_failure),
    )

    return result_dict


# ── Attempt fix (AI-assisted repair) ─────────────────────────────────

async def attempt_fix_step(
    session_id: str,
    tenant_id: str,
    step_index: int,
    error_details: str = "",
    external_log_cb=None,  # optional async callback(level, msg) for real-time streaming
) -> Dict[str, Any]:
    """Use AI to fix a failed step, then re-run it.

    1. Reads the error details from the previous execution result (or uses provided ones)
    2. Calls the appropriate fix handler (LLM generates corrected code)
    3. Re-runs the original step to validate the fix
    Returns the final execution result dict.
    """
    oid = ObjectId(session_id)
    # Use _id only — gateway injects tenant_id from JWT which may differ from stored session
    session = await sessions_collection().find_one({"_id": oid})
    if not session:
        raise ValueError(f"Session {session_id} not found")

    # Resolve real tenant from stored doc, not the (potentially JWT-overwritten) header
    tenant_id = session.get("tenant_id") or tenant_id

    # Propagate tenant_id and preferred model to LLM client ContextVars
    set_llm_tenant_id(tenant_id or "")
    set_llm_model(session.get("llm_model", "") or "")

    # Phase 4: rehydrate the full plan body before reading step config.
    await _hydrate_plan_from_r2(session)
    plan = session.get("plan", {})
    steps = plan.get("steps", [])
    if step_index < 0 or step_index >= len(steps):
        raise ValueError(f"Step index {step_index} out of range (0..{len(steps) - 1})")

    step = steps[step_index]
    if not isinstance(step, dict):
        raise ValueError(
            f"Step at index {step_index} is not a dict (got {type(step).__name__}: {str(step)[:80]}). "
            f"Session plan may be corrupted — re-generate the plan."
        )
    step_type = step.get("type", "")
    step_config = step.get("config", {})

    # Check if we have a fix handler for this step type
    fix_handler = FIX_HANDLERS.get(step_type)
    if not fix_handler:
        raise ValueError(f"No AI fix available for step type: {step_type}. Use retry instead.")

    # Smart handler override for run_tests happens after prev_result is parsed below

    provider = session["provider"]
    service = session["service"]
    catalog = get_service_detail(provider, service)
    if not catalog:
        _cn = session.get("connector_name") or f"{provider.title()} {service.replace('_', ' ').title()}"
        catalog = {
            "provider": provider, "service": service, "service_key": service,
            "display_name": _cn,
            "description": session.get("user_prompt") or f"Custom connector for {_cn}",
            "auth_type": session.get("auth_type") or "api_key",
            "category": "custom",
        }

    _connector_name = session.get("connector_name", "")
    service_slug = (
        _slug_from_connector_name(_connector_name)
        if _connector_name
        else _service_slug(provider, service)
    )
    class_name = _to_class_name(service)

    # Always initialise so they're defined even when error_details is provided by caller
    test_passed = 0
    test_failed = 0
    parsed_out: Dict[str, Any] = {}  # populated from prev_result output if available

    # Gather error details from previous execution if not provided
    if not error_details:
        existing_results = session.get("execution_results", [])
        prev_result = next(
            (r for r in existing_results if r.get("step_index") == step_index),
            None,
        )
        if prev_result:
            # Combine output + logs into error details
            parts = []
            raw_out = prev_result.get("output", "")
            if raw_out:
                parts.append(f"Output: {raw_out}")
            for log_entry in prev_result.get("logs", []):
                if log_entry.get("level") in ("error", "warn"):
                    parts.append(f"{log_entry['level'].upper()}: {log_entry['message']}")
            error_details = "\n".join(parts) if parts else "Step failed with unknown error"
            # Extract pytest counts + root_cause for smart fix routing
            if isinstance(raw_out, dict):
                parsed_out = raw_out
            elif isinstance(raw_out, str):
                try:
                    _decoded = json.loads(raw_out)
                    if isinstance(_decoded, dict):
                        parsed_out = _decoded
                    # else: json decoded to a scalar/list — leave parsed_out as {}
                except Exception:
                    pass
            test_passed = parsed_out.get("passed", 0)
            test_failed = parsed_out.get("failed", 0)

        # ── Fallback: run_tests step results are stored in test_results, not execution_results ──
        # The POST /test endpoint (called from UI "Run Tests" button) stores results in
        # session.test_results, not execution_results. When the UI then hits "Attempt Fix",
        # prev_result is None. Read test_results so smart routing can pick the right handler.
        elif step_type == "run_tests":
            _stored_tr = session.get("test_results", {})
            if _stored_tr:
                _pytest_out = _stored_tr.get("pytest", {})
                _tr_parts = []
                if _pytest_out.get("error"):
                    _tr_parts.append(f"Error: {_pytest_out['error']}")
                _raw_output = _pytest_out.get("output", "")
                if _raw_output:
                    _tr_parts.append(_raw_output[:3000])
                if _tr_parts:
                    error_details = "\n".join(_tr_parts)
                # Extract counts so smart routing can use them
                test_passed = _stored_tr.get("passed", 0) or _pytest_out.get("passed", 0)
                test_failed = _stored_tr.get("failed", 0) or _pytest_out.get("failed", 0)
                # Merge pytest sub-dict into parsed_out so root_cause + errors are accessible
                parsed_out = {**_pytest_out, **_stored_tr}

    # ── Extract test counts from raw pytest text when parsed_out had no counts ──
    # The auto-fix loop (handleRunTestStep) sends pytest.output (raw text), not JSON,
    # so test_failed stays 0 unless we regex-scan the error_details string.
    if not test_failed and error_details:
        import re as _re
        # Match both "41 failed" (pytest summary) and "41 test(s) failed" (frontend format)
        _m_fail = _re.search(r'(\d+)\s+(?:test\(s?\)\s+)?failed', error_details)
        _m_pass = _re.search(r'(\d+)\s+(?:test\(s?\)\s+)?passed', error_details)
        _m_err  = _re.search(r'(\d+)\s+(?:collection\s+)?error(?:s)?', error_details)
        if _m_fail:
            test_failed = int(_m_fail.group(1))
        if _m_pass and not test_passed:
            test_passed = int(_m_pass.group(1))
        # Also try to pick up collection errors for smart routing
        _text_errors = int(_m_err.group(1)) if _m_err else 0
    else:
        _text_errors = 0

    # ── Smart handler selection for run_tests (must be after parsed_out extraction) ──
    # Decide which fixer to use based on root_cause stored in the previous run's output.
    _smart_route_msg: Optional[str] = None
    if step_type == "run_tests":
        from integration.services.step_executor import (
            handle_fix_connector,
            handle_fix_tests,
            handle_fix_connector_for_tests,
        )
        root_cause = parsed_out.get("root_cause", "")
        _errors = parsed_out.get("errors", 0) or _text_errors

        # Detect test-structural errors in the error_details text that indicate
        # a broken TEST FILE (not a broken connector). These are test bugs:
        #   - patch.object() called with empty args → TypeError: object() takes no arguments
        #   - fixture not found → fixture 'mock_X' not found
        #   - import errors in test files
        # When present, always route to handle_fix_tests regardless of test_failed count.
        _TEST_STRUCTURE_PATTERNS = [
            "object() takes no arguments",         # patch.object() with empty args (old python)
            "_patch_object() missing",              # patch.object() with empty args (Python 3.14)
            "missing required positional argument", # missing args in patch/mock calls
            "class fixtures not supported",         # @pytest.fixture inside a class body
            "valueerror: class",                    # same class fixture error (ValueError prefix)
            "fixture '",                            # missing fixture
            "fixture \"",                           # missing fixture (double-quote)
            "no module named",                      # import error in test
            "cannot import",                        # import error in test
            "modulenotfounderror",                  # module not found in test
            "importerror",                          # generic import error
        ]
        _error_details_lower = (error_details or "").lower()
        _has_test_structure_error = any(p in _error_details_lower for p in _TEST_STRUCTURE_PATTERNS)

        # Detect assertion-too-strict failures: AssertionError where actual output
        # CONTAINS the expected string (connector IS producing the right output,
        # just with more context than the test assertion expects).
        # These are test bugs, not connector bugs — route to handle_fix_tests.
        _assertion_too_strict = False
        if "assertionerror" in _error_details_lower or "assert " in _error_details_lower:
            import re as _re_route
            # Pattern: assert 'X' in [...] where X appears inside the actual list as a substring
            _strict_matches = _re_route.findall(
                r"assert '([^']+)' in \[([^\]]+)\]", error_details or "", _re_route.DOTALL
            )
            for _expected, _actual_list in _strict_matches:
                # If the expected string appears inside any element of the actual list
                if _expected.lower() in _actual_list.lower():
                    _assertion_too_strict = True
                    break

        # How many consecutive connector fixes have been attempted without success?
        _connector_fix_attempts = _mem.get("fix_attempts", {}).get("run_tests", 0)

        if root_cause in ("connector_invalid", "connector_missing"):
            fix_handler = handle_fix_connector
            _smart_route_msg = "Smart routing: connector_invalid → fixing connector.py structure"
        elif root_cause in ("tests_invalid", "tests_missing"):
            fix_handler = handle_fix_tests
            _smart_route_msg = "Smart routing: tests_invalid → fixing test_connector.py structure"
        elif _has_test_structure_error:
            fix_handler = handle_fix_tests
            _smart_route_msg = "Smart routing: test structural error (patch/import/fixture bug) → fixing test file"
        elif _assertion_too_strict:
            fix_handler = handle_fix_tests
            _smart_route_msg = "Smart routing: assertion too strict (actual contains expected) → relaxing test assertions"
        elif test_failed > 0 or root_cause == "test_failures":
            # After 2+ failed connector fixes with no progress, switch to fixing tests
            if _connector_fix_attempts >= 2 and _connector_fix_attempts % 2 == 0:
                fix_handler = handle_fix_tests
                _smart_route_msg = f"Smart routing: {_connector_fix_attempts} connector fix attempts failed → switching to test assertion fix"
            else:
                fix_handler = handle_fix_connector_for_tests
                _smart_route_msg = f"Smart routing: {test_failed} test failure(s) → TDD fix for connector.py"
        elif _errors > 0:
            fix_handler = handle_fix_tests
            _smart_route_msg = f"Smart routing: {_errors} collection error(s) → fixing test_connector.py"
        else:
            # Fallback: run_tests step always means tests ran and failed behaviourally
            # → TDD fix (connector must be fixed to pass its tests)
            fix_handler = handle_fix_connector_for_tests
            _smart_route_msg = "Smart routing: run_tests fallback → TDD fix for connector.py"

    elif step_type == "write_tests":
        # ── Smart routing for write_tests (individual Fix button from unit tests accordion) ──
        # The unit-tests Fix button sends step_type=write_tests. By default this routes to
        # handle_fix_tests which calls gemini_agentic_fix and BLOCKS connector.py.
        # But test failures are often caused by connector bugs (ImportError, wrong import path,
        # etc.). Use handle_fix_connector_for_tests (unrestricted) whenever errors point to
        # connector issues or there are test failures, so Gemini can fix both files freely.
        from integration.services.step_executor import (
            handle_fix_tests,
            handle_fix_connector_for_tests,
        )
        _error_details_lower_wt = (error_details or "").lower()

        # Patterns that indicate the error is inside connector.py, not the test file
        _CONNECTOR_IMPORT_PATTERNS = [
            "attempted relative import",    # from .client import X → relative import error
            "importerror",                  # generic ImportError in connector
            "modulenotfounderror",          # missing module in connector
            "no module named",              # missing module in connector
        ]
        _connector_import_err = any(p in _error_details_lower_wt for p in _CONNECTOR_IMPORT_PATTERNS)

        # Patterns that indicate a STRUCTURAL bug in the test file itself
        _TEST_ONLY_PATTERNS = [
            "object() takes no arguments",
            "_patch_object() missing",
            "class fixtures not supported",
            "valueerror: class",
            "fixture '",
            'fixture "',
        ]
        _test_structural_err = any(p in _error_details_lower_wt for p in _TEST_ONLY_PATTERNS)

        if _test_structural_err and not _connector_import_err:
            # Pure structural test bug (patch/fixture syntax) with no connector error → test file only
            _smart_route_msg = "Smart routing: write_tests structural error → fixing test file only"
        else:
            # Everything else: use unrestricted fix so Gemini can edit connector.py,
            # exceptions.py, tests/, or any file needed. connector.py is NEVER blocked
            # when the user clicks Fix from the unit tests accordion.
            fix_handler = handle_fix_connector_for_tests
            _smart_route_msg = (
                "Smart routing: write_tests + connector import error → unrestricted fix"
                if _connector_import_err
                else f"Smart routing: write_tests → unrestricted fix (connector.py + tests both writable)"
            )

    # ── Load prior failure history for LLM context ────────────────────────
    prior_failure = await failure_tracker.get_failure_context(
        session_id=session_id,
        step_index=step_index,
        provider=provider,
        service=service,
        tenant_id=tenant_id,
    )
    failure_context_str = failure_tracker.build_failure_context_for_llm(prior_failure)
    # Inject into error_details so all fix handlers see the history
    if failure_context_str:
        error_details = f"{failure_context_str}\n\nCurrent Error:\n{error_details}"

    # Build fix attempt number + previous strategy summaries so Gemini doesn't repeat mistakes
    _fix_history = session.get("fix_history", {})
    _step_fix_key = f"{step_type}_{step_index}"
    _prev_summaries = _fix_history.get(_step_fix_key, [])
    _fix_attempt_num = len(_prev_summaries) + 1

    context = {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "provider": provider,
        "service": service,
        "service_slug": service_slug,
        "service_name": catalog["display_name"],
        "connector_name": session.get("connector_name", ""),
        "class_name": class_name,
        "auth_type": catalog.get("auth_type", "unknown"),
        "sdk_package": catalog.get("sdk_package", ""),
        "docs_url": catalog.get("docs_url", ""),
        "default_scopes": catalog.get("default_scopes", []),
        "user_prompt": session.get("user_prompt", ""),
        "run_kind": session.get("run_kind", "build"),
        "parent_session_id": session.get("parent_session_id", ""),
        "is_enhance": session.get("run_kind") == "enhance",
        "error_details": error_details,
        "test_passed": test_passed,
        "test_failed": test_failed,
        "fix_attempt": _fix_attempt_num,
        "previous_fix_summaries": _prev_summaries,
        "package_structure": _build_package_structure(session.get("plan", {})),
        "failure_id": prior_failure.get("failure_id") if prior_failure else None,
    }

    # Mark step as executing + remove old result
    await sessions_collection().update_one(
        {"_id": oid},
        {
            "$set": {f"plan.steps.{step_index}.status": StepStatus.EXECUTING.value},
            "$pull": {"execution_results": {"step_index": step_index}},
        },
    )

    log_messages: List[Dict[str, str]] = []

    async def log_cb(level: str, msg: str):
        log_messages.append({"level": level, "message": msg})
        # Stream immediately to SSE if caller provided a real-time callback
        if external_log_cb:
            try:
                await external_log_cb(level, msg)
            except Exception:
                pass  # never let log streaming crash the fix

    # Emit smart routing decision (set before log_messages was available)
    if _smart_route_msg:
        await log_cb("info", _smart_route_msg)

    started = time.time()

    # Phase 1: Run the fix handler (AI generates corrected code)
    try:
        fix_result = await fix_handler(step_config, context, log_cb)
    except Exception as exc:
        fix_result = {"status": "fail", "output": str(exc)}
        log_messages.append({"level": "error", "message": f"Fix exception: {exc}"})

    # ── Persist fix attempt summary to session so next attempt knows what was tried ──
    _fix_summary = (
        f"Used {fix_handler.__name__} for {step_type} — "
        f"result: {fix_result.get('status', 'unknown')} — "
        f"error: {str(fix_result.get('output', ''))[:200]}"
    )
    _updated_summaries = _prev_summaries + [_fix_summary]
    asyncio.ensure_future(sessions_collection().update_one(
        {"_id": oid},
        {"$set": {f"fix_history.{_step_fix_key}": _updated_summaries[-5:]}},  # keep last 5
    ))

    _active_failure_id = prior_failure.get("failure_id") if prior_failure else None

    # ── RAG indexing after phase-1 fix (code has been rewritten) ──────
    # Index even if phase 2 hasn't run yet — next fix attempt needs the
    # latest connector state in the knowledge base immediately.
    # For run_tests fixes, map the fix handler to the correct ingest step type.
    _ingest_step_type: str | None = None  # also used in the auto-retry loop below
    if fix_result.get("status") == "pass":
        from integration.services.step_executor import handle_fix_connector, handle_fix_connector_for_tests
        if step_type in ("write_connector", "scaffold_code", "install_deps", "generate_metadata"):
            _ingest_step_type = step_type
        elif step_type == "write_tests":
            _ingest_step_type = "write_tests"
        elif step_type == "run_tests":
            # Fix handler rewrites connector.py or test files — ingest based on which handler ran
            if fix_handler in (handle_fix_connector, handle_fix_connector_for_tests):
                _ingest_step_type = "write_connector"
            else:
                _ingest_step_type = "write_tests"
        if _ingest_step_type:
            _fix_out_dir = _output_dir(tenant_id, service_slug)
            asyncio.ensure_future(_ingest_files_for_step(
                _fix_out_dir, _ingest_step_type, tenant_id, provider, service
            ))

    if fix_result.get("status") != "pass":
        # Phase 1 fix itself failed — append attempt record and return
        fix_details = str(fix_result.get("output", "")) + "\n" + "\n".join(
            m["message"] for m in log_messages if m.get("level") in ("error", "warn")
        )
        if _active_failure_id:
            asyncio.ensure_future(failure_tracker.append_fix_attempt(
                failure_id=_active_failure_id,
                provider=provider,
                service=service,
                tenant_id=tenant_id,
                outcome="failed",
                strategy=f"AI fix for {step_type} (phase 1 — code generation failed)",
                details=fix_details,
            ))
        else:
            # No prior failure recorded — create one now
            asyncio.ensure_future(failure_tracker.create_failure(
                session_id=session_id,
                step_index=step_index,
                step_type=step_type,
                provider=provider,
                service=service,
                tenant_id=tenant_id,
                error_summary=f"AI fix generation failed for {step_type}",
                full_output=fix_details,
            ))

        duration_ms = round((time.time() - started) * 1000, 1)
        exec_result = StepExecutionResult(
            step_index=step_index,
            status="fail",
            output=json.dumps(fix_result.get("output", ""))[:5000],
            duration_ms=duration_ms,
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
        result_dict = exec_result.model_dump(mode="json")
        result_dict["logs"] = log_messages[-100:]
        result_dict["fix_attempted"] = True
        slim_result = await _offload_exec_result_to_r2(
            provider=provider, service_slug=service_slug, session_id=session_id, result_dict=result_dict,
        )
        await sessions_collection().update_one(
            {"_id": oid},
            {
                "$set": {
                    f"plan.steps.{step_index}.status": StepStatus.FAILED.value,
                    "updated_at": datetime.utcnow(),
                },
                "$push": {"execution_results": slim_result},
            },
        )
        return result_dict

    # Phase 2: Re-run the original step to validate the fix.
    # Auto-retry loop for test steps: if tests still fail, extract new errors,
    # re-run the fix handler, and validate again (up to _MAX_AUTO_FIX_RETRIES times).
    _MAX_AUTO_FIX_RETRIES = 2  # up to 3 total fix attempts per click
    _auto_retry = 0

    # For write_tests, Phase 2 must NOT re-run handle_write_tests (full regeneration).
    # Full regeneration overwrites the Phase 1 fix and can introduce NEW syntax errors.
    # Instead, validate by running the tests directly (handle_run_tests).
    if step_type == "write_tests":
        from integration.services.step_executor import handle_run_tests as _handle_run_tests
        _validation_handler = _handle_run_tests
    else:
        _validation_handler = STEP_HANDLERS.get(step_type)

    result = fix_result  # fallback if no validation handler
    step_status = fix_result.get("status", "fail")

    while True:
        log_messages.append({"level": "info", "message": (
            f"Fix applied — re-running step to validate (attempt {_auto_retry + 1}/{_MAX_AUTO_FIX_RETRIES + 1})..."
        )})
        if _validation_handler:
            try:
                result = await _validation_handler(step_config, context, log_cb)
            except Exception as exc:
                result = {"status": "fail", "output": str(exc)}
                log_messages.append({"level": "error", "message": f"Re-run exception: {exc}"})
        else:
            result = fix_result

        step_status = result.get("status", "fail")

        # Done if passed or not a test step (no retry benefit for non-test steps)
        if step_status == "pass":
            break
        if _auto_retry >= _MAX_AUTO_FIX_RETRIES:
            break
        if step_type not in ("write_tests", "run_tests"):
            break  # Only auto-retry test-related steps

        # ── Still failing: extract new pytest errors and retry fix ────────────
        _auto_retry += 1
        _run_output = result.get("output", {})
        if isinstance(_run_output, dict):
            _new_error_text = (
                _run_output.get("stdout", "")
                or _run_output.get("output", "")
                or str(_run_output)
            )[:3000]
        else:
            _new_error_text = str(_run_output)[:3000]

        context["error_details"] = (
            f"Fix attempt {_auto_retry} STILL FAILING. New pytest output:\n{_new_error_text}"
        )
        context["fix_attempt"] = context.get("fix_attempt", 1) + 1

        await log_cb(
            "warn",
            f"⚠ Tests still failing — auto-retrying fix "
            f"({_auto_retry}/{_MAX_AUTO_FIX_RETRIES})...",
        )

        # Re-run phase 1 fix with updated error context
        try:
            fix_result = await fix_handler(step_config, context, log_cb)
        except Exception as exc:
            fix_result = {"status": "fail", "output": str(exc)}
            log_messages.append({"level": "error", "message": f"Auto-retry fix exception: {exc}"})

        if fix_result.get("status") != "pass":
            await log_cb("error", f"Fix handler failed on auto-retry {_auto_retry} — stopping")
            result = fix_result
            step_status = "fail"
            break

        # RAG index the newly fixed code before next validation round
        if _ingest_step_type:
            _retry_out_dir = _output_dir(tenant_id, service_slug)
            asyncio.ensure_future(_ingest_files_for_step(
                _retry_out_dir, _ingest_step_type, tenant_id, provider, service
            ))

    duration_ms = round((time.time() - started) * 1000, 1)

    # ── Append fix attempt + resolve/keep failure ─────────────────────────
    phase2_details = str(result.get("output", "")) + "\n" + "\n".join(
        m["message"] for m in log_messages[-20:] if m.get("level") in ("error", "warn", "info")
    )
    if _active_failure_id:
        if step_status == "pass":
            asyncio.ensure_future(failure_tracker.append_fix_attempt(
                failure_id=_active_failure_id,
                provider=provider,
                service=service,
                tenant_id=tenant_id,
                outcome="succeeded",
                strategy=f"AI fix for {step_type} (re-run passed)",
                details=phase2_details,
            ))
            asyncio.ensure_future(failure_tracker.resolve_failure(
                session_id=session_id,
                step_index=step_index,
                provider=provider,
                service=service,
                tenant_id=tenant_id,
            ))
        else:
            asyncio.ensure_future(failure_tracker.append_fix_attempt(
                failure_id=_active_failure_id,
                provider=provider,
                service=service,
                tenant_id=tenant_id,
                outcome="failed",
                strategy=f"AI fix for {step_type} (re-run still failing)",
                details=phase2_details,
            ))
    elif step_status != "pass":
        # No prior failure doc — create one for this new failure
        asyncio.ensure_future(failure_tracker.create_failure(
            session_id=session_id,
            step_index=step_index,
            step_type=step_type,
            provider=provider,
            service=service,
            tenant_id=tenant_id,
            error_summary=f"Step still failing after fix attempt for {step_type}",
            full_output=phase2_details,
        ))

    exec_result = StepExecutionResult(
        step_index=step_index,
        status=step_status,
        output=json.dumps(result.get("output", ""))[:5000],
        duration_ms=duration_ms,
        started_at=datetime.utcnow(),
        finished_at=datetime.utcnow(),
    )
    result_dict = exec_result.model_dump(mode="json")
    result_dict["logs"] = log_messages[-100:]
    result_dict["fix_attempted"] = True

    final_step_status = StepStatus.COMPLETED.value if step_status == "pass" else StepStatus.FAILED.value
    _step_set: dict = {
        f"plan.steps.{step_index}.status": final_step_status,
        "updated_at": datetime.utcnow(),
    }
    # When run_tests fix succeeds, also mark write_tests step as completed so the UI
    # step indicator turns green (it reads plan.steps.N.status).
    if step_status == "pass" and step_type == "run_tests":
        for _i, _s in enumerate(steps):
            if isinstance(_s, dict) and _s.get("type") == "write_tests":
                _step_set[f"plan.steps.{_i}.status"] = StepStatus.COMPLETED.value
    slim_result = await _offload_exec_result_to_r2(
        provider=provider, service_slug=service_slug, session_id=session_id, result_dict=result_dict,
    )
    await sessions_collection().update_one(
        {"_id": oid},
        {
            "$set": _step_set,
            "$push": {"execution_results": slim_result},
        },
    )

    logger.info(
        "execution.fix_step_finished",
        session_id=session_id,
        step_index=step_index,
        step_type=step_type,
        status=step_status,
        duration_ms=duration_ms,
        fix_attempted=True,
    )

    return result_dict


# ── Auto-run session (run remaining, fix failures, no re-runs) ────

async def auto_run_session(
    session_id: str,
    tenant_id: str,
) -> AsyncGenerator[str, None]:
    """Automatically execute all remaining steps, fixing failures along the way.

    - COMPLETED steps are skipped (no re-runs, no duplicate LLM calls)
    - FAILED steps: attempt AI fix first, then re-run
    - PENDING steps: run normally
    - Critical failures (write_connector) stop the auto-run

    Yields SSE events for real-time progress.
    """
    logger.info("auto_run.starting", session_id=session_id, tenant_id=tenant_id)

    oid = ObjectId(session_id)
    session = await sessions_collection().find_one({"_id": oid, "tenant_id": tenant_id})
    if not session:
        yield _sse_event("error", {"message": f"Session {session_id} not found"})
        return

    # Phase 4: rehydrate the full plan body before reading step config.
    await _hydrate_plan_from_r2(session)
    plan = session.get("plan", {})
    steps = plan.get("steps", [])
    if not steps:
        yield _sse_event("error", {"message": "No steps in plan"})
        return

    provider = session["provider"]
    service = session["service"]
    catalog = get_service_detail(provider, service)
    if not catalog:
        _cn = session.get("connector_name") or f"{provider.title()} {service.replace('_', ' ').title()}"
        catalog = {
            "provider": provider, "service": service, "service_key": service,
            "display_name": _cn,
            "description": session.get("user_prompt") or f"Custom connector for {_cn}",
            "auth_type": session.get("auth_type") or "api_key",
            "category": "custom",
        }

    _connector_name = session.get("connector_name", "")
    service_slug = (
        _slug_from_connector_name(_connector_name)
        if _connector_name
        else _service_slug(provider, service)
    )
    class_name = _to_class_name(service)

    context = {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "provider": provider,
        "service": service,
        "service_slug": service_slug,
        "service_name": catalog["display_name"],
        "class_name": class_name,
        "auth_type": catalog.get("auth_type", "unknown"),
        "sdk_package": catalog.get("sdk_package", ""),
        "docs_url": catalog.get("docs_url", ""),
        "default_scopes": catalog.get("default_scopes", []),
        "user_prompt": session.get("user_prompt", ""),
        "run_kind": session.get("run_kind", "build"),
        "parent_session_id": session.get("parent_session_id", ""),
        "is_enhance": session.get("run_kind") == "enhance",
        "package_structure": _build_package_structure(plan),
    }

    # Set session to executing and persist service_slug so testing_service can find files
    update_result = await sessions_collection().update_one(
        {"_id": oid, "status": {"$ne": SessionStatus.EXECUTING.value}},
        {"$set": {"status": SessionStatus.EXECUTING.value, "service_slug": service_slug, "updated_at": datetime.utcnow()}},
    )
    if update_result.modified_count == 0:
        # Status is already "executing" — force-reset (stale from a previous crashed run)
        await sessions_collection().update_one(
            {"_id": oid},
            {"$set": {"status": SessionStatus.EXECUTING.value, "service_slug": service_slug, "updated_at": datetime.utcnow()}},
        )
        logger.warning("auto_run.force_reset_stale_executing", session_id=session_id)

    yield _sse_event("auto_run_start", {
        "session_id": session_id,
        "step_count": len(steps),
        "service": catalog["display_name"],
    })

    existing_results = session.get("execution_results", [])
    stats = {"skipped": 0, "run": 0, "fixed": 0, "failed": 0}
    all_passed = True

    # Load durable execution state from R2 (survives across sessions)
    exec_state = await r2_service.get_execution_state(provider, service_slug, tenant_id) or {}
    r2_completed: List[str] = exec_state.get("completed_steps", [])

    # If connector.py doesn't exist, bypass R2 cache and reset all step statuses
    out_dir_check = _output_dir(tenant_id, service_slug)
    if r2_completed and not (out_dir_check / "connector.py").exists():
        logger.warning(
            "auto_run.connector_missing_clearing_r2",
            session_id=session_id, out_dir=str(out_dir_check),
        )
        r2_completed = []
        reset_update = {"updated_at": datetime.utcnow()}
        for idx in range(len(steps)):
            reset_update[f"plan.steps.{idx}.status"] = StepStatus.PENDING.value
        await sessions_collection().update_one({"_id": oid}, {"$set": reset_update})
        steps = [{**s, "status": StepStatus.PENDING.value} for s in steps]
        yield _sse_event("step_log", {
            "step_index": -1, "level": "warn",
            "message": "Generated files not found on disk — ignoring R2 cache, running all steps fresh",
        })

    for i, step in enumerate(steps):
        step_type = step.get("type", "")
        step_title = step.get("title", f"Step {i + 1}")
        step_config = step.get("config", {})
        step_status_val = step.get("status", "")

        # write_tests requires manual user action — STOP auto-run here, notify UI
        if step_type == "write_tests":
            yield _sse_event("step_manual_required", {
                "step_index": i,
                "step_type": step_type,
                "title": step_title,
                "message": "Write Unit Tests requires manual execution — select methods and click Generate Test Cases in the accordion.",
            })
            logger.info("auto_run.step_manual_required", session_id=session_id, step_index=i, step_type=step_type)
            break  # stop auto-run — run_tests cannot proceed without the test file

        # version_upgrade requires user input — pause execution until user selects version
        if step_type == "version_upgrade":
            # Use the last confirmed version from MongoDB as the true "current" version.
            # The regenerated connector.json always starts at "1.0.0", so reading only
            # from the file would lose track of previous bumps (e.g. 1.0.1 → shows 1.0.0).
            db_version = session.get("metadata_version") or ""
            meta_path = _output_dir(tenant_id, service_slug) / "metadata" / "connector.json"
            file_version = "1.0.0"
            try:
                if meta_path.exists():
                    _meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
                    file_version = str(_meta_data.get("version", "1.0.0"))
            except Exception:
                pass
            # Prefer the DB version (reflects previous bumps); fall back to file version
            current_version = db_version if db_version else file_version

            suggestions = _compute_version_suggestions(current_version)

            await sessions_collection().update_one(
                {"_id": oid},
                {"$set": {
                    f"plan.steps.{i}.status": StepStatus.PENDING_VERSION.value,
                    "version_upgrade_pending": {
                        "current_version": current_version,
                        "suggestions": suggestions,
                        "step_index": i,
                    },
                }},
            )

            yield _sse_event("version_upgrade_required", {
                "step_index": i,
                "current_version": current_version,
                "suggestions": suggestions,
                "message": "Select a version number to release this connector.",
            })

            logger.info("auto_run.version_upgrade_pending",
                        session_id=session_id, step_index=i, current_version=current_version)
            break  # pause — resumed by select-version API endpoint

        # ── SKIP completed steps (strict no re-run) ──
        # Check MongoDB status (this session) OR R2 durable state (prior sessions)
        if step_status_val == StepStatus.COMPLETED.value or step_type in r2_completed:
            stats["skipped"] += 1
            skip_reason = (
                "Already completed — skipping"
                if step_status_val == StepStatus.COMPLETED.value
                else "Completed in prior session (R2) — skipping"
            )
            # Ensure MongoDB step status is "completed" (may still be "pending" from prior session)
            if step_status_val != StepStatus.COMPLETED.value:
                await sessions_collection().update_one(
                    {"_id": oid},
                    {"$set": {f"plan.steps.{i}.status": StepStatus.COMPLETED.value}},
                )
            yield _sse_event("step_skipped", {
                "step_index": i, "step_type": step_type, "title": step_title,
                "message": skip_reason,
            })
            continue

        # ── FAILED steps: attempt fix first ──
        fix_succeeded = False
        fix_handler = None  # reset each iteration
        if step_status_val == StepStatus.FAILED.value:
            # Gather error details from previous result
            prev_result = next(
                (r for r in existing_results if r.get("step_index") == i), None,
            )
            err_details = ""
            root_cause = ""
            if prev_result:
                parts = []
                raw_output = prev_result.get("output", "")
                # Parse output — may be a JSON string with root_cause
                parsed_output = {}
                if isinstance(raw_output, str):
                    try:
                        parsed_output = json.loads(raw_output)
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif isinstance(raw_output, dict):
                    parsed_output = raw_output

                root_cause = parsed_output.get("root_cause", "") if isinstance(parsed_output, dict) else ""
                if raw_output:
                    parts.append(f"Output: {raw_output}")
                for log_entry in prev_result.get("logs", []):
                    if log_entry.get("level") in ("error", "warn"):
                        parts.append(f"{log_entry['level'].upper()}: {log_entry['message']}")
                err_details = "\n".join(parts) if parts else "Step failed"

            # Expose pytest counts so fix handler can decide whether to regenerate or patch
            context["test_passed"] = parsed_output.get("passed", 0) if isinstance(parsed_output, dict) else 0
            context["test_failed"] = parsed_output.get("failed", 0) if isinstance(parsed_output, dict) else 0

            # ── Smart fix routing ──
            # For run_tests: ALWAYS try quick __init__.py sync first (zero LLM cost)
            if step_type == "run_tests":
                out_dir = _output_dir(tenant_id, service_slug)
                actual_class = _sync_init_with_connector(out_dir, context)
                if actual_class:
                    expected_class = context.get("class_name", "Connector")
                    if actual_class != expected_class:
                        # Update context so subsequent steps use correct class name
                        context["class_name"] = actual_class
                        yield _sse_event("step_log", {
                            "step_index": i, "level": "info",
                            "message": f"Quick fix: synced __init__.py class '{expected_class}' → '{actual_class}'",
                        })
                    # Mark as quick-fixed — step will re-run below automatically
                    fix_succeeded = True
                    stats["fixed"] += 1
                    yield _sse_event("step_fixed", {
                        "step_index": i, "step_type": step_type, "title": step_title,
                        "message": f"Quick fix applied — class synced to '{actual_class}'",
                    })

            # If quick fix didn't apply (non-run_tests or sync failed), route to LLM fix
            if not fix_succeeded:
                if step_type == "run_tests" and root_cause in ("connector_invalid", "connector_import_error"):
                    # Connector is broken — fix the connector
                    fix_handler = FIX_HANDLERS.get("write_connector")
                elif step_type == "run_tests" and root_cause in ("tests_invalid", "tests_import_error", "collection_error"):
                    # Structural test issue — fix test setup only (imports, class names)
                    fix_handler = FIX_HANDLERS_STRUCTURAL.get("run_tests")
                elif step_type == "run_tests" and root_cause == "test_failures":
                    # TDD: tests define the contract — fix the CONNECTOR to pass the tests
                    fix_handler = FIX_HANDLERS.get("run_tests")  # → handle_fix_connector_for_tests
                else:
                    fix_handler = FIX_HANDLERS.get(step_type)

                if fix_handler:
                    yield _sse_event("step_fixing", {
                        "step_index": i, "step_type": step_type, "title": step_title,
                        "message": f"Root cause: {root_cause or 'unknown'} — calling AI to fix...",
                    })
                    fix_context = {**context, "error_details": err_details,
                                   "test_passed": context.get("test_passed", 0),
                                   "test_failed": context.get("test_failed", 0)}
                    fix_log: List[Dict[str, str]] = []

                    async def fix_log_cb(level: str, msg: str, _fix_log=fix_log):
                        _fix_log.append({"level": level, "message": msg})

                    try:
                        fix_result = await fix_handler(step_config, fix_context, fix_log_cb)
                        if fix_result.get("status") == "pass":
                            fix_succeeded = True
                            stats["fixed"] += 1
                            yield _sse_event("step_fixed", {
                                "step_index": i, "step_type": step_type, "title": step_title,
                                "message": "AI fix applied successfully",
                            })
                        else:
                            yield _sse_event("step_log", {
                                "step_index": i, "level": "warn",
                                "message": "AI fix did not succeed — will attempt original handler anyway",
                            })
                    except Exception as exc:
                        fix_log.append({"level": "error", "message": f"Fix exception: {exc}"})

        # ── Pre-run dependency check for run_tests ──
        if step_type == "run_tests" and not fix_succeeded:
            out_dir = _output_dir(tenant_id, service_slug)

            # Always sync __init__.py class name with connector.py (quick, no LLM)
            actual_class = _sync_init_with_connector(out_dir, context)
            expected_class = context.get("class_name", "Connector")
            if actual_class and actual_class != expected_class:
                context["class_name"] = actual_class  # Keep context up to date
                yield _sse_event("step_log", {
                    "step_index": i, "level": "info",
                    "message": f"Pre-run: synced __init__.py class '{expected_class}' → '{actual_class}'",
                })

            file_status = validate_generated_files(out_dir)
            if not file_status["connector"]["valid"]:
                reason = file_status["connector"]["reason"]
                yield _sse_event("step_log", {
                    "step_index": i, "level": "warn",
                    "message": f"connector.py is invalid ({reason}) — attempting connector fix before running tests...",
                })
                connector_fix = FIX_HANDLERS.get("write_connector")
                if connector_fix:
                    cfix_log: List[Dict[str, str]] = []

                    async def cfix_log_cb(level: str, msg: str, _cl=cfix_log):
                        _cl.append({"level": level, "message": msg})

                    try:
                        cfix_ctx = {**context, "error_details": f"connector.py is {reason}"}
                        cfix_result = await connector_fix(step_config, cfix_ctx, cfix_log_cb)
                        if cfix_result.get("status") == "pass":
                            stats["fixed"] += 1
                            yield _sse_event("step_log", {
                                "step_index": i, "level": "success",
                                "message": "connector.py fixed successfully — proceeding to run tests",
                            })
                    except Exception as exc:
                        yield _sse_event("step_log", {
                            "step_index": i, "level": "error",
                            "message": f"Connector fix failed: {exc}",
                        })

        # ── Run step with automatic retry (max 2 attempts for run_tests) ──
        handler = STEP_HANDLERS.get(step_type)
        if not handler:
            yield _sse_event("step_log", {"step_index": i, "level": "error", "message": f"Unknown step type: {step_type}"})
            all_passed = False
            stats["failed"] += 1
            continue

        max_attempts = 3 if step_type == "smoke_test" else 2 if step_type == "run_tests" else 1
        step_status = "fail"
        result = {"status": "fail", "output": ""}
        duration_ms = 0.0
        log_messages: List[Dict[str, str]] = []

        # Define log_cb once — uses the same log_messages list (cleared per attempt)
        async def log_cb(level: str, msg: str, _lm=log_messages):
            _lm.append({"level": level, "message": msg})

        for attempt in range(max_attempts):
            if attempt > 0:
                yield _sse_event("step_log", {
                    "step_index": i, "level": "info",
                    "message": f"Retry attempt {attempt + 1}/{max_attempts} for {step_title}...",
                })
                # On retry, re-sync __init__.py and try LLM fix based on last failure
                out_dir_retry = _output_dir(tenant_id, service_slug)
                synced_class = _sync_init_with_connector(out_dir_retry, context)
                # Update context with actual class name (CRITICAL #3 fix)
                if synced_class:
                    context["class_name"] = synced_class

                # Parse the failure output and route to appropriate LLM fix
                retry_output = result.get("output", "")
                retry_root_cause = ""
                if isinstance(retry_output, str):
                    try:
                        retry_parsed = json.loads(retry_output) if retry_output.startswith("{") else {}
                        retry_root_cause = retry_parsed.get("root_cause", "")
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif isinstance(retry_output, dict):
                    retry_root_cause = retry_output.get("root_cause", "")

                retry_fix_handler = None
                if step_type == "smoke_test":
                    # Always route smoke test failures to handle_fix_smoke_test (fast-fix + LLM)
                    retry_fix_handler = FIX_HANDLERS.get("smoke_test")
                elif retry_root_cause in ("connector_invalid", "connector_import_error"):
                    retry_fix_handler = FIX_HANDLERS.get("write_connector")
                elif retry_root_cause in ("tests_invalid", "tests_import_error", "collection_error"):
                    # Structural test issues only — fix test setup
                    retry_fix_handler = FIX_HANDLERS_STRUCTURAL.get("run_tests")
                elif retry_root_cause == "test_failures":
                    # TDD: tests are the spec — fix the connector to pass them
                    retry_fix_handler = FIX_HANDLERS.get("run_tests")  # → handle_fix_connector_for_tests

                if retry_fix_handler:
                    retry_err = "\n".join(f"{l['level']}: {l['message']}" for l in log_messages if l.get("level") in ("error", "warn"))
                    # Also include the full step output as error context for smoke test
                    if step_type == "smoke_test":
                        smoke_output = result.get("output", "")
                        if smoke_output:
                            retry_err = f"{smoke_output}\n{retry_err}".strip()
                        context = {**context, "error_details": retry_err}
                    yield _sse_event("step_fixing", {
                        "step_index": i, "step_type": step_type, "title": step_title,
                        "message": f"Auto-fixing smoke test failure (attempt {attempt + 1})..." if step_type == "smoke_test" else f"Retry: root_cause={retry_root_cause} — calling AI fix...",
                    })
                    retry_fix_log: List[Dict[str, str]] = []
                    async def retry_fix_cb(level: str, msg: str, _rfl=retry_fix_log):
                        _rfl.append({"level": level, "message": msg})
                    try:
                        _retry_fix_attempt = context.get("fix_attempt", 1) + 1
                        rfix = await retry_fix_handler(step_config, {**context, "error_details": retry_err, "fix_attempt": _retry_fix_attempt}, retry_fix_cb)
                        if rfix.get("status") == "pass":
                            stats["fixed"] += 1
                            yield _sse_event("step_fixed", {
                                "step_index": i, "step_type": step_type, "title": step_title,
                                "message": f"AI fix applied on retry (root_cause: {retry_root_cause})",
                            })
                        for rl in retry_fix_log:
                            yield _sse_event("step_log", {"step_index": i, "level": rl["level"], "message": rl["message"]})
                    except Exception as rexc:
                        yield _sse_event("step_log", {"step_index": i, "level": "error", "message": f"Retry fix exception: {rexc}"})

            # Run the actual step
            yield _sse_event("step_start", {
                "step_index": i, "step_type": step_type, "title": step_title,
            })

            await sessions_collection().update_one(
                {"_id": oid},
                {
                    "$set": {f"plan.steps.{i}.status": StepStatus.EXECUTING.value},
                    "$pull": {"execution_results": {"step_index": i}},
                },
            )

            log_messages.clear()  # Clear the SAME list — closure keeps correct reference

            started = time.time()
            try:
                result = await handler(step_config, context, log_cb)
            except Exception as exc:
                result = {"status": "fail", "output": str(exc)}
                log_messages.append({"level": "error", "message": f"Exception: {exc}"})

            duration_ms = round((time.time() - started) * 1000, 1)

            for log_entry in log_messages:
                yield _sse_event("step_log", {
                    "step_index": i, "level": log_entry["level"], "message": log_entry["message"],
                })

            step_status = result.get("status", "fail")

            if step_status == "pass":
                break  # Success — exit retry loop

            # If we have more attempts, continue the loop (will apply fix + retry)
            if attempt < max_attempts - 1:
                yield _sse_event("step_log", {
                    "step_index": i, "level": "warn",
                    "message": f"Step failed — will attempt automatic fix and retry...",
                })

        # ── Validate step output before marking completed in MongoDB ──
        validation = {"valid": True, "reason": "skipped (step did not pass)"}
        if step_status == "pass":
            out_dir_val = _output_dir(tenant_id, service_slug)
            validation = validate_step_output(step_type, out_dir_val, result)
            if not validation["valid"]:
                step_status = "fail"
                yield _sse_event("step_log", {
                    "step_index": i, "level": "error",
                    "message": f"Validation failed — {validation['reason']}",
                })
            else:
                yield _sse_event("step_log", {
                    "step_index": i, "level": "success",
                    "message": f"Validation passed — {validation['reason']}",
                })

        # Persist final result
        exec_result = StepExecutionResult(
            step_index=i,
            status=step_status,
            output=json.dumps(result.get("output", ""))[:5000],
            duration_ms=duration_ms,
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
        result_dict = exec_result.model_dump(mode="json")
        result_dict["logs"] = log_messages[-100:]
        result_dict["fix_attempted"] = fix_succeeded
        result_dict["validation"] = validation

        final_step_status = StepStatus.COMPLETED.value if step_status == "pass" else StepStatus.FAILED.value
        slim_result = await _offload_exec_result_to_r2(
            provider=provider, service_slug=service_slug, session_id=session_id, result_dict=result_dict,
        )
        await sessions_collection().update_one(
            {"_id": oid},
            {
                "$set": {f"plan.steps.{i}.status": final_step_status, "updated_at": datetime.utcnow()},
                "$push": {"execution_results": slim_result},
            },
        )

        yield _sse_event("step_complete", {
            "step_index": i, "status": step_status,
            "duration_ms": duration_ms,
            "output_preview": str(result.get("output", ""))[:500],
            "validation": validation,
        })

        if step_status == "pass":
            stats["run"] += 1
            # Persist durable execution state to R2/local cache (survives new sessions)
            if step_type not in r2_completed:
                r2_completed = r2_completed + [step_type]
            await r2_service.save_execution_state(provider, service_slug, tenant_id, r2_completed, session_id)
            out_dir_tl = _output_dir(tenant_id, service_slug)
            _append_timeline(out_dir_tl, session_id, step_title, i, step_status, duration_ms)
        else:
            all_passed = False
            stats["failed"] += 1
            # Stop on critical failure
            if step_type in ("write_connector",):
                yield _sse_event("auto_stopped", {
                    "step_index": i, "title": step_title,
                    "message": f"Critical step '{step_title}' failed — auto-run stopped",
                })
                break

    # Final status
    out_dir = _output_dir(tenant_id, service_slug)
    quality = analyze_directory(str(out_dir))
    generated_files = []
    if out_dir.exists():
        for py_file in out_dir.rglob("*.py"):
            rel = str(py_file.relative_to(out_dir))
            generated_files.append(GeneratedFile(
                path=rel,
                size=py_file.stat().st_size,
                language="python",
                quality_score=next(
                    (f["quality_score"] for f in quality.get("files", []) if f.get("path") == rel),
                    None,
                ),
            ).model_dump())

    final_status = SessionStatus.COMPLETED.value if all_passed else SessionStatus.FAILED.value
    final_set_auto: Dict[str, Any] = {
        "status": final_status,
        "generated_files": generated_files,
        "updated_at": datetime.utcnow(),
    }
    # Store LLM-generated name in a separate field; never overwrite the user's connector_name.
    try:
        connector_json_path = _output_dir(tenant_id, service_slug) / "metadata" / "connector.json"
        if connector_json_path.exists():
            _meta = json.loads(connector_json_path.read_text())
            if _meta.get("name"):
                final_set_auto["generated_connector_name"] = _meta["name"]
    except Exception:
        pass
    await sessions_collection().update_one(
        {"_id": oid},
        {"$set": final_set_auto},
    )

    logger.info(
        "auto_run.complete",
        session_id=session_id,
        stats=stats,
        status="completed" if all_passed else "failed",
    )

    yield _sse_event("auto_complete", {
        "session_id": session_id,
        "status": "completed" if all_passed else "failed",
        "stats": stats,
        "file_count": len(generated_files),
        "average_quality_score": quality.get("average_quality_score", 0),
    })
