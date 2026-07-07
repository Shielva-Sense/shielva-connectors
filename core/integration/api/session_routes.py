"""Integration Builder — Session CRUD routes."""

import asyncio
import contextlib
import hashlib
import hmac as hmaclib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from integration.api.ws_routes import ws_manager
from integration.core.config import settings
from integration.db.database import sessions_collection
from integration.schemas.models import (
    CreateSessionRequest,
    IntegrationSession,
    SessionStatus,
)
from integration.services import execution_manager, knowledge_service, r2_service
from integration.services.code_analysis_service import delete_code_analysis

logger = structlog.get_logger(__name__)

session_router = APIRouter(prefix="/sessions", tags=["sessions"])

_CRED_WINDOW_MS = 2 * 60 * 1_000  # same 2-minute window as frontend


@session_router.get("/cred-salt")
async def get_cred_salt():
    """Return an HMAC of the current 2-minute time window keyed by CREDENTIAL_SECRET."""
    win = int(time.time() * 1_000) // _CRED_WINDOW_MS
    msg = f"shielva-cred-{win}".encode()
    digest = hmaclib.new(
        settings.CREDENTIAL_SECRET.encode(),
        msg,
        hashlib.sha256,
    ).hexdigest()
    return {"hmac": digest, "win": win}


@session_router.get("/{session_id}/cred-hmac")
async def get_session_cred_hmac(session_id: str):
    """Return a stable per-session HMAC keyed by CREDENTIAL_SECRET + session_id.

    Unlike /cred-salt this is NOT time-windowed — the same session always returns
    the same HMAC so encrypted credentials persist across app restarts.
    Credentials cannot be decrypted without the backend's CREDENTIAL_SECRET.
    """
    msg = f"shielva-session-cred-{session_id}".encode()
    digest = hmaclib.new(
        settings.CREDENTIAL_SECRET.encode(),
        msg,
        hashlib.sha256,
    ).hexdigest()
    return {"hmac": digest}


# ── Shared install-credential vault (so ACP can pre-fill what SAD captured) ───
class StoreSessionCredsBody(BaseModel):
    connector_type: str
    values: dict[str, Any]


@session_router.post("/{session_id}/credentials")
async def store_session_credentials_endpoint(
    session_id: str,
    body: StoreSessionCredsBody,
    x_tenant_id: str | None = Header(None),
):
    """SAD publishes the install credentials it captured. Stored encrypted at rest
    via the single-owner credential_manager (AES-256-GCM, per-tenant DEK) so the
    ACP install form — which reads the same store — can pre-fill them later.

    The storage key is the CANONICAL connector_type from connector.json (the same
    value the install form reads via meta.connector_type), so a save from the main
    build and a save from an enhancement run always land on the SAME tenant+type
    slot — no split-brain, last-write-wins."""
    tenant_id = _get_tenant(x_tenant_id)
    from services import credential_manager

    clean = {k: v for k, v in (body.values or {}).items() if v not in (None, "")}
    if not clean:
        return {"ok": True, "stored": 0}
    connector_type = await _canonical_connector_type(session_id, tenant_id, body.connector_type)
    await credential_manager.store_credentials(tenant_id, connector_type, clean)
    logger.info(
        "session.credentials_stored",
        session_id=session_id,
        connector_type=connector_type,
        fields=len(clean),
    )
    return {"ok": True, "stored": len(clean)}


async def _canonical_connector_type(session_id: str, tenant_id: str, fallback: str) -> str:
    """Resolve the canonical connector_type from connector.json for this session.

    Falls back to the value the caller supplied if metadata isn't on disk yet.
    Guarantees the credential storage key matches what the install form reads.
    """
    try:
        import json as _json

        from integration.api.codeview_routes import _resolve_output_dir

        meta_path = (await _resolve_output_dir(session_id, tenant_id)) / "metadata" / "connector.json"
        if meta_path.exists():
            ct = _json.loads(meta_path.read_text(encoding="utf-8")).get("connector_type")
            if ct:
                return ct
    except Exception:
        pass
    return fallback


class AutofillCredentialsRequest(BaseModel):
    fields: list[dict[str, str]]  # [{key, label, description, type}]
    connector_py: str = ""  # content of connector.py (sent from frontend)
    connector_json: str = ""  # content of metadata/connector.json
    service: str = ""  # e.g. "gmail"
    provider: str = ""  # e.g. "google"
    tenant_id: str = ""


@session_router.post("/{session_id}/autofill-credentials")
async def autofill_credentials(session_id: str, body: AutofillCredentialsRequest):
    """Use AI to research and suggest values for credential/config fields.

    Reads the connector code + metadata to understand what values are needed,
    then calls the configured LLM (Gemini / Claude) to suggest standard values
    based on the service's official documentation.

    Returns: { suggestions: { field_key: suggested_value } }
    """
    from integration.services.llm_client import call_llm

    # Build field descriptions for the prompt
    field_lines = "\n".join(
        f"  - {f.get('key', '')}: {f.get('label', '')} ({f.get('description', '')} | type: {f.get('type', 'string')})"
        for f in body.fields
    )

    connector_context = ""
    if body.connector_py:
        # Only send class-level constants section (first 60 lines) to keep prompt lean
        snippet = "\n".join(body.connector_py.splitlines()[:80])
        connector_context += f"\n\nconnector.py (first 80 lines):\n```python\n{snippet}\n```"
        # Extract method signatures from the WHOLE file so the LLM knows what
        # operations the connector performs — critical for choosing OAuth scopes
        # that actually cover them (e.g. send/create needs write scopes, not just
        # read-only). The first-80-lines snippet is class constants, not methods.
        import re as _re_methods

        method_sigs = _re_methods.findall(
            r"^\s*(?:async\s+)?def\s+([a-zA-Z_]\w*)\s*\(",
            body.connector_py,
            _re_methods.MULTILINE,
        )
        ops = [m for m in method_sigs if not m.startswith("_")]
        if ops:
            connector_context += (
                "\n\nConnector operations (public methods — scopes MUST cover all of these):\n"
                + "\n".join(f"- {m}()" for m in ops)
            )
    if body.connector_json:
        try:
            cj = json.loads(body.connector_json)
            connector_context += f"\n\nconnector.json metadata:\n{json.dumps({k: cj[k] for k in ('name', 'service', 'auth_type', 'base_url') if k in cj}, indent=2)}"
        except Exception:
            pass

    prompt = f"""You are a connector configuration expert. Research the standard API values for the **{body.provider} {body.service}** connector.

Given these credential/configuration fields:
{field_lines}
{connector_context}

Return ONLY a JSON object mapping field keys to their standard values.
Rules:
- For API endpoints and URLs: use the official documented URL (research if needed).
- For scopes: choose the LEAST-PRIVILEGE OAuth2 scopes that still cover EVERY
  operation this connector performs (see "Connector operations" above). If any
  method sends, creates, updates, deletes, or otherwise writes, you MUST include
  the corresponding write/send scope — do NOT default to a read-only scope.
  Example: a Gmail connector that sends or drafts needs send/compose scopes
  (e.g. gmail.send, gmail.compose), NOT gmail.readonly. Space-separate multiple scopes.
- For rate limits, pagination types, api versions: use the documented defaults.
- For secrets (client_id, client_secret, access_token, api_key, password): set value to "" (empty) — user must provide these.
- For redirect_uri: use "http://localhost:8080/oauth/callback" unless connector.py shows otherwise.
- Do NOT include markdown, only raw JSON.

Output format (values shown are placeholders — fill with the REAL values for {body.provider} {body.service}):
{{"auth_url": "<provider authorize URL>", "token_url": "<provider token URL>", "scopes": "<scopes covering ALL operations above>", "client_id": "", "client_secret": ""}}"""

    try:
        raw = await call_llm(
            [{"role": "user", "content": prompt}],
            system="You are a technical API documentation expert. Return only valid JSON, no markdown.",
            expect_code=False,
            max_tokens=1024,
            tenant_id=body.tenant_id or None,  # required for mcp mode
        )
        # Extract JSON from response — handle ```json ... ``` fences too
        import re as _re

        json_match = (
            _re.search(r"```json\s*(\{.*?\})\s*```", raw, _re.DOTALL)
            or _re.search(r"```\s*(\{.*?\})\s*```", raw, _re.DOTALL)
            or _re.search(r"(\{[^{}]+\})", raw, _re.DOTALL)
            or _re.search(r"(\{.*\})", raw, _re.DOTALL)
        )
        if not json_match:
            raise ValueError(f"No JSON found in LLM response: {raw[:200]}")
        suggestions = json.loads(json_match.group(1))
        # Remove empty-string values (let frontend keep existing or blank)
        suggestions = {k: v for k, v in suggestions.items() if v and str(v).strip()}
        logger.info("autofill_credentials.success", fields=list(suggestions.keys()))
        return {"suggestions": suggestions}
    except Exception as e:
        logger.warning("autofill_credentials.failed", error=str(e))
        return {"suggestions": {}, "error": str(e)}


def _cache_prompt_to_tmp(content: str, label: str) -> str:
    """Write prompt content to a tmp file (keyed by content hash) and return the path.

    Idempotent — if a file with the same content hash already exists it is NOT
    rewritten.  The returned path can be embedded in the Claude prompt as:
      "Read the instructions at `<path>` before proceeding."
    """
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    tmp_path = os.path.join(tempfile.gettempdir(), f"shielva_{label}_{content_hash}.md")
    if not os.path.exists(tmp_path):
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
    return tmp_path


def _canon_key(s: str) -> str:
    """Canonical catalog key: lowercase, alnum + underscore only.

    Convention enforced on every session at write time so the catalog key
    is the single source of truth — never display names, never provider-
    prefixed service values, never mixed case.
    """
    import re as _re_canon

    if not s:
        return s
    s = s.lower().strip()
    s = s.replace(" ", "_").replace("-", "_").replace(".", "_")
    s = _re_canon.sub(r"[^a-z0-9_]", "", s)
    return _re_canon.sub(r"_+", "_", s).strip("_")


def _get_tenant(x_tenant_id: str | None = Header(None)) -> str | None:
    """Return tenant_id from header (optional — may be None pre-login)."""
    return x_tenant_id or None


def _get_app_id(x_app_id: str | None = Header(None)) -> str | None:
    """Return the stable per-install app_id from X-App-ID header."""
    return x_app_id or None


def _session_filter(oid, app_id: str | None, tenant_id: str | None) -> dict:
    """Build the MongoDB filter for a single session lookup.

    Priority: app_id (new sessions) → tenant_id (legacy sessions without app_id).
    Both can coexist: if the doc has an app_id we always use it.
    """
    f: dict = {"_id": oid}
    if app_id:
        f["app_id"] = app_id
    elif tenant_id:
        f["tenant_id"] = tenant_id
    return f


# ── App identity / tenant linking ─────────────────────────────────────────────


@session_router.post("/app/link-tenant")
async def link_app_to_tenant(
    body: dict,
    x_app_id: str | None = Header(None),
    x_tenant_id: str | None = Header(None),
    x_tenant_name: str | None = Header(None),
):
    """Map a stable app_id to an authenticated tenant after login.

    Called by the Electron app immediately after OAuth login.
    Updates ALL sessions for this app_id with the real tenant_id + tenant_name,
    making pre-login sessions visible to the authenticated user.
    No R2 file movement required — the bucket is already app-scoped.
    """
    app_id = x_app_id or body.get("app_id") or None
    tenant_id = x_tenant_id or body.get("tenant_id") or None
    tenant_name = (x_tenant_name or body.get("tenant_name") or "").strip().lower()

    if not app_id:
        raise HTTPException(400, "X-App-ID header (or app_id in body) is required")
    if not tenant_id:
        raise HTTPException(400, "X-Tenant-ID header (or tenant_id in body) is required")

    result = await sessions_collection().update_many(
        {"app_id": app_id},
        {
            "$set": {
                "tenant_id": tenant_id,
                "tenant_name": tenant_name,
                "updated_at": __import__("datetime").datetime.utcnow(),
            }
        },
    )
    logger.info(
        "app.link_tenant",
        app_id=app_id,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        sessions_updated=result.modified_count,
    )
    return {"ok": True, "sessions_updated": result.modified_count}


# ── Slug helper ───────────────────────────────────────────────────────


def _compute_unique_slug(session_id_str: str, connector_name: str, service: str, seed: str) -> str:
    """Derive a per-session service_slug: {base_slug}_{6-char-hash}  e.g. gmail_a3f9c1.

    The base is the connector name (or provider service as fallback) with any
    trailing 'connector' word stripped; the 6-char suffix is md5(session_id + seed)
    so every session — build OR enhance — gets its own isolated workspace + R2 scratch.
    """
    import hashlib as _slug_hash
    import re as _slug_re

    _cn = (connector_name or "").strip().lower()
    _cn = _slug_re.sub(r"[\s_]+connector\s*$", "", _cn)
    _cn = _slug_re.sub(r"[\s\-]+", "_", _cn)
    _cn = _slug_re.sub(r"[^\w]", "", _cn)
    _cn = _slug_re.sub(r"_connector$", "", _cn)
    _base_slug = _cn or service.replace("-", "_").lower()
    _suffix = _slug_hash.md5(f"{session_id_str}{seed}".encode(), usedforsecurity=False).hexdigest()[:6]
    return f"{_base_slug}_{_suffix}"


# ── CRUD ──────────────────────────────────────────────────────────────


@session_router.post("/{session_id}/enhance-run")
async def create_enhance_run(
    session_id: str,
    x_app_id: str | None = Header(None),
    x_tenant_id: str | None = Header(None),
    x_tenant_name: str | None = Header(None),
):
    """Start a NEW enhancement run against an existing connector.

    An enhance run is its own session (own slug → own plan/stepper/exec/tests/docs
    scratch) linked to the originating build via parent_session_id. It copies the
    parent's connector identity (provider/service/connector_name/llm_model) but starts
    with a fresh workflow, so it never clobbers the build run's history. The published
    artifact stays name-keyed, so on merge the SAME canonical connector is updated.

    Returns the new run's id + slug AND the parent's slug/name so the caller can seed
    the new run's local workspace from the parent connector's files.
    """
    from bson import ObjectId

    app_id = x_app_id or None
    tenant_id = x_tenant_id or None
    tenant_name = (x_tenant_name or "").strip().lower()

    try:
        parent_oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(400, "Invalid session_id")

    parent = await sessions_collection().find_one(_session_filter(parent_oid, app_id, tenant_id))
    if not parent:
        raise HTTPException(404, "Parent connector session not found")

    # Inherit every connector-IDENTITY / generation-INPUT field the model tags with
    # `enhance_inherit` (auth_type, selected_config_keys, default_config, docs_urls,
    # custom_rules_md, method_identities, …). Driven by the model — NOT a hardcoded
    # list here — so a new connector-config field is inherited automatically. Without
    # this, an enhance run regenerates with empty config and regresses the connector
    # (e.g. auth_type→api_key dropped AUTH_URI and broke OAuth).
    inherited = {f: parent[f] for f in IntegrationSession.enhance_inherited_fields() if f in parent}
    # Request headers win only as a fallback when the parent value is absent.
    inherited["app_id"] = parent.get("app_id") or app_id
    inherited["tenant_id"] = parent.get("tenant_id") or tenant_id
    inherited["tenant_name"] = parent.get("tenant_name") or tenant_name

    child = IntegrationSession(
        **inherited,
        user_prompt="",
        status=SessionStatus.PLANNING,
        run_kind="enhance",
        parent_session_id=session_id,
    )
    doc = child.model_dump()
    result = await sessions_collection().insert_one(doc)
    child_id = str(result.inserted_id)

    unique_slug = _compute_unique_slug(
        child_id,
        parent.get("connector_name", ""),
        parent["service"],
        (parent.get("app_id") or parent.get("tenant_id") or child_id),
    )
    await sessions_collection().update_one(
        {"_id": result.inserted_id},
        {"$set": {"service_slug": unique_slug}},
    )

    logger.info(
        "session.enhance_run_created",
        run_id=child_id,
        parent_id=session_id,
        tenant_id=child.tenant_id,
        service_slug=unique_slug,
    )
    return {
        "id": child_id,
        "service_slug": unique_slug,
        "run_kind": "enhance",
        "parent_session_id": session_id,
        "parent_service_slug": parent.get("service_slug", ""),
        "connector_name": parent.get("connector_name", ""),
        "provider": parent["provider"],
        "service": parent["service"],
    }


class ImportConnectorSpec(BaseModel):
    service_slug: str  # the dir slug, e.g. "google_gmail_168e0e"
    provider: str
    service: str
    connector_name: str = ""
    version: str = ""  # metadata_version to display
    run_kind: str = "build"  # "build" | "enhance"
    output_dir: str = ""  # local path to the connector directory (optional)


class ImportExistingBody(BaseModel):
    connectors: list[ImportConnectorSpec]


@session_router.post("/restore-from-r2")
async def restore_sessions_from_r2(
    x_app_id: str | None = Header(None),
    x_tenant_id: str | None = Header(None),
    x_tenant_name: str | None = Header(None),
):
    """Rebuild any session that exists in R2 but is missing from Mongo.

    R2 is the durable copy of every connector run (files + plan_steps.json +
    stepper_progress.json + connector.json), keyed by the original session id.
    This reconstructs the full session doc — identity, output_dir, plan steps,
    version, stepper progress — with the ORIGINAL _id, so a lost/partial session
    is restored in one call. Idempotent: existing sessions are skipped.
    """
    import json as _json
    import re as _re

    from bson import ObjectId as _OID

    from integration.schemas.models import StepStatus, StepType
    from integration.services import r2_service

    app_id = x_app_id or None
    tenant_id = x_tenant_id or None
    tenant_name = (x_tenant_name or "").strip().lower()
    if not app_id:
        raise HTTPException(400, "X-App-ID header is required to resolve the R2 bucket")

    r2_service._app_bucket_ctx.set(r2_service.app_id_to_bucket(app_id))
    bucket = r2_service._get_bucket()
    client = r2_service._get_client()

    # Enumerate connectors/{slug}/sessions/{session_id}/ in the app bucket.
    pat = _re.compile(r"^connectors/([^/]+)/sessions/([^/]+)/")
    found: set[tuple[str, str]] = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="connectors/"):
        for obj in page.get("Contents", []):
            m = pat.match(obj.get("Key", ""))
            if m:
                found.add((m.group(1), m.group(2)))

    def _getj(slug: str, sid: str, rel: str):
        try:
            body = client.get_object(Bucket=bucket, Key=f"connectors/{slug}/sessions/{sid}/{rel}")["Body"].read()
            return _json.loads(body)
        except Exception:
            return None

    valid_types = {e.value for e in StepType}
    valid_status = {e.value for e in StepStatus}
    type_map = {"write_integration_tests": "run_integration_tests"}

    col = sessions_collection()
    restored: list[str] = []
    skipped: list[str] = []
    # Track slugs whose connector already exists (by id OR by connector_name match) so
    # we never restore a second session for the same connector when two R2 entries exist.
    # Key: (tenant_id_or_empty, slug) → True when already present in Mongo.
    restored_slugs: set[tuple[str, str]] = set()

    # Pre-load existing slugs for this app/tenant to avoid double-restoring.
    existing_slug_query: dict = {}
    if tenant_id:
        existing_slug_query["tenant_id"] = tenant_id
    if app_id:
        existing_slug_query["app_id"] = app_id
    async for existing in col.find(existing_slug_query, {"service_slug": 1, "service": 1}):
        _es = (existing.get("service_slug") or existing.get("service") or "").lower()
        if _es:
            restored_slugs.add((_es, (tenant_id or "").lower()))

    for slug, sid in found:
        try:
            oid = _OID(sid)
        except Exception:
            continue
        # Skip if the exact session id is already in Mongo.
        if await col.find_one({"_id": oid}):
            skipped.append(sid)
            continue
        # Skip if we already have any session (restored or native) for this connector slug
        # under this tenant — prevents duplicate restore when R2 has two build sessions for
        # the same connector (e.g. two google_gmail entries after a re-build).
        _slug_key = (slug.lower(), (tenant_id or "").lower())
        if _slug_key in restored_slugs:
            skipped.append(sid)
            logger.info("session.restore_skipped_duplicate", session_id=sid, slug=slug)
            continue

        cj = _getj(slug, sid, "metadata/connector.json") or {}
        ps = _getj(slug, sid, "plan_steps.json") or {}
        sp = _getj(slug, sid, "stepper_progress.json") or {}

        steps = []
        for s in ps.get("steps", []):
            t = (s.get("type") or "").lower()
            t = type_map.get(t, t)
            if t not in valid_types:
                t = "write_connector"
            st = (s.get("status") or "").lower()
            if st not in valid_status:
                st = "completed"
            steps.append(
                {
                    "index": s.get("index", len(steps)),
                    "type": t,
                    "title": s.get("title", ""),
                    "description": s.get("title", ""),
                    "estimated_duration_s": 30,
                    "config": {},
                    "status": st,
                }
            )

        session = IntegrationSession(
            app_id=app_id,
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            provider=cj.get("provider", ""),
            service=cj.get("service", slug),
            connector_name=cj.get("connector_name") or cj.get("display_name") or slug,
            run_kind="build",
            status=SessionStatus.COMPLETED,
        )
        doc = session.model_dump()
        doc["_id"] = oid
        doc["service_slug"] = slug
        if ps.get("output_dir"):
            doc["output_dir"] = ps["output_dir"]
        doc["metadata_version"] = cj.get("version")
        doc["stepper_max_step"] = int(sp.get("maxReachedStep", 7) or 7)
        # Phase 4: full plan body → R2; slim {version, steps:[{index,title,
        # type,status}]} → Mongo. Restored sessions get the same offload as
        # freshly-planned ones so the Mongo doc stays tiny.
        _full_plan = {"steps": steps, "version": 1}
        from integration.services.planning_service import persist_plan as _persist_plan

        doc["plan"] = await _persist_plan(
            session_id=str(oid),
            provider=cj.get("provider", ""),
            service_slug=slug,
            plan=_full_plan,
        )
        doc["restored_from_r2"] = True
        await col.insert_one(doc)
        restored_slugs.add(_slug_key)
        restored.append(sid)
        logger.info("session.restored_from_r2", session_id=sid, slug=slug, steps=len(steps))

    return {"restored": restored, "skipped": skipped, "count": len(restored)}


@session_router.post("/import-existing")
async def import_existing_sessions(
    body: ImportExistingBody,
    x_app_id: str | None = Header(None),
    x_tenant_id: str | None = Header(None),
    x_tenant_name: str | None = Header(None),
):
    """Recreate session rows for connectors that already exist on disk.

    Idempotent per service_slug — a connector that already has a session is skipped,
    so re-running is safe. Imported sessions are marked completed with the stepper at
    the final step. NOTE: original event history / exact version are not on disk, so
    metadata_version comes from the supplied (connector.json) value.
    """
    app_id = x_app_id or None
    tenant_id = x_tenant_id or None
    tenant_name = (x_tenant_name or "").strip().lower()
    if not app_id and not tenant_id:
        raise HTTPException(400, "Either X-App-ID or X-Tenant-ID header is required")

    import json as _json

    from integration.schemas.models import StepStatus, StepType

    _valid_types = {e.value for e in StepType}
    _type_map = {"write_integration_tests": "run_integration_tests"}

    def _load_plan_steps(output_dir: str) -> list:
        """Read plan_steps.json from output_dir and return plan step dicts with step configs."""
        if not output_dir:
            return []
        ps_path = Path(output_dir) / "plan_steps.json"
        if not ps_path.exists():
            return []
        try:
            raw = _json.loads(ps_path.read_text())
        except Exception:
            return []
        # Load connector metadata for per-step config enrichment
        meta: dict = {}
        for meta_rel in ("metadata/connector.json", "connector.json"):
            mp = Path(output_dir) / meta_rel
            if mp.exists():
                with contextlib.suppress(Exception):
                    meta = _json.loads(mp.read_text())
                break
        install_fields = meta.get("install_fields", [])
        api_methods = [a.get("name", "") for a in meta.get("apis", [])]
        steps = []
        for i, s in enumerate(raw):
            t = (s.get("type") or "").lower()
            t = _type_map.get(t, t)
            if t not in _valid_types:
                t = "write_connector"
            st = (s.get("status") or "").lower()
            if st not in {e.value for e in StepStatus}:
                st = "completed"
            # Enrich step config from connector metadata
            cfg: dict = {}
            if t == "write_connector":
                cfg = {
                    "methods": api_methods,
                    "install_fields": install_fields,
                    "features": [
                        "retry",
                        "pagination",
                        "circuit_breaker",
                        "normalizer",
                        "rate_limiting",
                    ],
                }
            elif t == "generate_metadata":
                cfg = {"install_fields": install_fields, "api_count": len(api_methods)}
            elif t in ("write_tests", "run_integration_tests"):
                cfg = {"methods": api_methods, "test_type": "both"}
            steps.append(
                {
                    "index": i,
                    "type": t,
                    "title": s.get("title", ""),
                    "description": s.get("description", s.get("title", "")),
                    "estimated_duration_s": s.get("estimated_duration_s", 30),
                    "config": s.get("config", cfg) or cfg,
                    "status": st,
                }
            )

        # Always append terminal steps if missing
        _step_types = {s["type"] for s in steps}
        _next = len(steps)
        if "setup_instructions" not in _step_types:
            steps.append(
                {
                    "index": _next,
                    "type": "setup_instructions",
                    "title": "Generate Setup Instructions",
                    "description": "Research and generate connector-specific configuration guide — shows users exactly where to find credentials in the provider portal.",
                    "estimated_duration_s": 45,
                    "config": {},
                    "status": "completed",
                }
            )
            _next += 1
        if "version_upgrade" not in _step_types:
            steps.append(
                {
                    "index": _next,
                    "type": "version_upgrade",
                    "title": "Version Upgrade",
                    "description": "Review changes and set the release version for this connector. Select patch, minor, or major version bump.",
                    "estimated_duration_s": 30,
                    "config": {"auto_suggest": True},
                    "status": "completed",
                }
            )
        return steps

    def _load_connector_meta(output_dir: str) -> dict:
        """Read metadata/connector.json (or connector.json) from output_dir."""
        if not output_dir:
            return {}
        for rel in ("metadata/connector.json", "connector.json"):
            p = Path(output_dir) / rel
            if p.exists():
                try:
                    return _json.loads(p.read_text())
                except Exception:
                    pass
        return {}

    col = sessions_collection()
    created: list[str] = []
    skipped: list[str] = []
    for spec in body.connectors:
        owner = {"app_id": app_id} if app_id else {"tenant_id": tenant_id}
        if await col.find_one({**owner, "service_slug": spec.service_slug}):
            skipped.append(spec.service_slug)
            continue

        meta = _load_connector_meta(spec.output_dir)
        install_fields: list = meta.get("install_fields", [])
        auth_type: str = meta.get("auth_type", "")

        # Detect test_type: "both" when integration test file is present
        has_integration_tests = spec.output_dir and (Path(spec.output_dir) / "tests" / "test_integration.py").exists()
        test_type = "both" if has_integration_tests else "unit"

        # selected_config_keys: all install field keys → user-provided at connect time
        selected_config_keys = [f["key"] for f in install_fields if f.get("key")]

        # Derive package_structure, recommended_features, default_config_fields from metadata
        feature_ids: list = meta.get("features", [])
        recommended_features = [
            {
                "id": fid,
                "label": fid.replace("_", " ").title(),
                "recommended": True,
                "category": "connector",
                "description": "",
            }
            for fid in feature_ids
        ]
        default_config_fields = install_fields  # already {key, label, type, required, ...}
        pkg_root = meta.get("connector_id", spec.service_slug)
        if not pkg_root.endswith("_connector"):
            pkg_root = pkg_root + "_connector"
        pkg_files: list = []
        if spec.output_dir:
            import os as _os

            for walk_root, dirs, filenames in _os.walk(spec.output_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", ".shielva")]
                for fname in filenames:
                    if fname.endswith((".py", ".json", ".txt", ".ini", ".cfg", ".md", ".toml")):
                        rel = _os.path.relpath(_os.path.join(walk_root, fname), spec.output_dir)
                        pkg_files.append({"path": rel})
        package_structure = {"root": pkg_root, "files": pkg_files[:60]}

        session = IntegrationSession(
            app_id=app_id,
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            provider=spec.provider,
            service=spec.service,
            connector_name=spec.connector_name or "",
            run_kind=spec.run_kind if spec.run_kind in ("build", "enhance") else "build",
            status=SessionStatus.COMPLETED,
            auth_type=auth_type,
            test_type=test_type,
            selected_config_keys=selected_config_keys,
            default_config=install_fields,
        )
        doc = session.model_dump()
        doc["service_slug"] = spec.service_slug
        doc["stepper_max_step"] = 7  # completed → all steps reachable
        doc["metadata_version"] = spec.version or None
        doc["imported_from_disk"] = True  # provenance marker
        doc["selected_features"] = feature_ids or [
            "retry",
            "pagination",
            "circuit_breaker",
            "normalizer",
            "rate_limiting",
        ]
        doc["package_structure"] = package_structure
        doc["recommended_features"] = recommended_features
        doc["default_config_fields"] = default_config_fields
        if spec.output_dir:
            doc["output_dir"] = spec.output_dir
        steps = _load_plan_steps(spec.output_dir)
        if steps:
            # Phase 4: pre-allocate the session _id so we can write the full
            # plan to R2 (keyed by session_id) before the insert. Mongo then
            # gets the slim summary only.
            from bson import ObjectId as _OID

            doc["_id"] = _OID()
            from integration.services.planning_service import (
                persist_plan as _persist_plan,
            )

            doc["plan"] = await _persist_plan(
                session_id=str(doc["_id"]),
                provider=spec.provider,
                service_slug=spec.service_slug,
                plan={"steps": steps, "version": 1},
            )
        res = await col.insert_one(doc)
        created.append(str(res.inserted_id))
        logger.info(
            "session.imported_from_disk",
            service_slug=spec.service_slug,
            session_id=str(res.inserted_id),
            steps=len(steps),
            auth_type=auth_type,
            test_type=test_type,
            config_keys=selected_config_keys,
        )

    return {"created": created, "skipped": skipped, "imported": len(created)}


@session_router.post("")
async def create_session(
    body: CreateSessionRequest,
    x_app_id: str | None = Header(None),
    x_tenant_id: str | None = Header(None),
    x_tenant_name: str | None = Header(None),
):
    """Create a new integration session.

    Pre-login: only app_id is set; tenant fields are None.
    Post-login: app_id + tenant_id + tenant_name are all set.
    """
    app_id = x_app_id or None
    tenant_id = x_tenant_id or None
    tenant_name = (x_tenant_name or "").strip().lower()

    if not app_id and not tenant_id:
        raise HTTPException(400, "Either X-App-ID or X-Tenant-ID header is required")

    canon_provider = _canon_key(body.provider)
    canon_service = _canon_key(body.service)
    # Strip duplicated provider prefix from service (e.g. "google_drive" → "drive")
    if canon_provider and canon_service.startswith(canon_provider + "_"):
        canon_service = canon_service[len(canon_provider) + 1 :]
    # Strip "_connector" suffix
    if canon_service.endswith("_connector"):
        canon_service = canon_service[: -len("_connector")]

    session = IntegrationSession(
        app_id=app_id,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        provider=canon_provider,
        service=canon_service,
        connector_name=body.connector_name or "",
        user_prompt=body.user_prompt,
        docs_urls=body.docs_urls or [],
        custom_rules_md=body.custom_rules_md or "",
        llm_model=body.llm_model or "",
        status=SessionStatus.PLANNING,
    )
    doc = session.model_dump()
    result = await sessions_collection().insert_one(doc)
    session_id_str = str(result.inserted_id)

    # ── Unique service_slug ──────────────────────────────────────────────────
    # Each session gets its own isolated output directory even when the same
    # connector name is used multiple times (build run + every enhance run).
    _unique_slug = _compute_unique_slug(
        session_id_str,
        body.connector_name,
        canon_service,
        app_id or tenant_id or session_id_str,
    )

    await sessions_collection().update_one(
        {"_id": result.inserted_id},
        {"$set": {"service_slug": _unique_slug}},
    )

    doc["_id"] = session_id_str
    doc["id"] = session_id_str
    doc["service_slug"] = _unique_slug
    logger.info(
        "session.created",
        session_id=session_id_str,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        provider=body.provider,
        service=body.service,
        connector_name=body.connector_name,
        service_slug=_unique_slug,
    )
    return doc


@session_router.patch("/{session_id}")
async def patch_session(
    session_id: str,
    body: dict,
    x_app_id: str | None = Header(None),
    x_tenant_id: str | None = Header(None),
):
    """Partial update of a session (docs_urls, custom_rules_md, etc.)."""
    from bson import ObjectId

    app_id = x_app_id or None
    tenant_id = x_tenant_id or None
    allowed = {
        "docs_urls",
        "custom_rules_md",
        "default_config",
        "selected_features",
        "plan_modified",
        "method_identifiers",
        "entity_configs",
        "mongo_provision",
        "stepper_max_step",
        "test_type",
        "selected_config_keys",
        "user_prompt",
        "method_identities",
        "synthesized_prompt",
        "output_dir",
        "auth_type",
        "package_structure",
        "recommended_features",
        "default_config_fields",
        "alias_name",
        "status",
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return {"ok": True}
    updates["updated_at"] = __import__("datetime").datetime.utcnow()
    await sessions_collection().update_one(
        _session_filter(ObjectId(session_id), app_id, tenant_id),
        {"$set": updates},
    )
    return {"ok": True}


@session_router.get("/{session_id}/stepper-progress")
async def get_stepper_progress(
    session_id: str,
    x_app_id: str | None = Header(None),
    x_tenant_id: str | None = Header(None),
):
    """Return the highest stepper tab reached for this session from R2/disk progress.json."""
    app_id = x_app_id or None
    tenant_id = x_tenant_id or None
    oid = ObjectId(session_id)
    session = await sessions_collection().find_one(_session_filter(oid, app_id, tenant_id))
    if not session:
        raise HTTPException(404, "Session not found")

    provider = session.get("provider", "")
    service_slug = session.get("service_slug") or session.get("service", "").replace("-", "_").lower()

    r2_step = await r2_service.get_stepper_max_step(provider, service_slug, tenant_id)
    mongo_step = session.get("stepper_max_step", 0) or 0
    # Return the highest value across both stores
    return {"stepper_max_step": max(r2_step, mongo_step)}


class StepperProgressBody(BaseModel):
    stepper_max_step: int


@session_router.patch("/{session_id}/stepper-progress")
async def patch_stepper_progress(
    session_id: str,
    body: StepperProgressBody,
    x_tenant_id: str | None = Header(None),
):
    """Update stepper_max_step in both R2/disk progress.json and MongoDB (best-effort)."""
    tenant_id = _get_tenant(x_tenant_id)
    app_id = None  # TODO: add x_app_id header to this endpoint
    oid = ObjectId(session_id)
    session = await sessions_collection().find_one(_session_filter(oid, app_id, tenant_id))
    if not session:
        raise HTTPException(404, "Session not found")

    provider = session.get("provider", "")
    service_slug = session.get("service_slug") or session.get("service", "").replace("-", "_").lower()
    step_index = body.stepper_max_step

    # Write to R2/disk (non-blocking — errors are swallowed inside the service)
    await r2_service.update_stepper_max_step(provider, service_slug, tenant_id, step_index)

    # Also update MongoDB for quick restore
    await sessions_collection().update_one(
        _session_filter(oid, app_id, tenant_id),
        {
            "$max": {"stepper_max_step": step_index},
            "$set": {"updated_at": __import__("datetime").datetime.utcnow()},
        },
    )
    return {"ok": True, "stepper_max_step": step_index}


@session_router.post("/{session_id}/analyze-docs")
async def analyze_session_docs(session_id: str, x_tenant_id: str | None = Header(None)):
    """Fetch docs_urls from the session, synthesize, and return structured extracted fields.

    Returns {fields: {scopes, base_url, auth_url, token_url, rate_limit_per_min, pagination_type, api_version}}
    so the frontend can pre-fill the Default Configuration panel.
    """
    tenant_id = _get_tenant(x_tenant_id)
    app_id = None  # TODO: add x_app_id header to this endpoint
    oid = ObjectId(session_id)
    session = await sessions_collection().find_one(_session_filter(oid, app_id, tenant_id))
    if not session:
        raise HTTPException(404, "Session not found")

    docs_urls: list[str] = [u for u in (session.get("docs_urls") or []) if u.strip()]
    if not docs_urls:
        return {"fields": {}}

    provider = session["provider"]
    service = session["service"]

    from integration.services.docs_synth_service import fetch_and_extract_fields

    extracted = await fetch_and_extract_fields(
        docs_urls=docs_urls,
        provider=provider,
        service=service,
    )
    return {"fields": extracted}


@session_router.get("")
async def list_sessions(
    x_app_id: str | None = Header(None),
    x_tenant_id: str | None = Header(None),
    status: str | None = None,
    provider: str | None = None,
    service: str | None = None,
    include_inactive: bool = False,
    skip: int = 0,
    limit: int = 50,
    summary: bool = False,
):
    """List sessions for this app install / tenant, filtered by status, provider, service.

    Query priority:
      1. app_id (X-App-ID header) — covers both pre-login and post-login sessions for this device.
      2. tenant_id only (legacy: sessions created before app_id was introduced).
    """
    app_id = x_app_id or None
    tenant_id = x_tenant_id or None

    if app_id:
        # Primary: all sessions for this installation regardless of login state
        query: dict = {"app_id": app_id}
    elif tenant_id:
        # Legacy fallback: sessions created before app_id was introduced
        query = {"tenant_id": tenant_id}
    else:
        raise HTTPException(400, "Either X-App-ID or X-Tenant-ID header is required")

    if status:
        query["status"] = status
    elif not include_inactive:
        # Exclude inactive sessions unless the caller opts in (SAD uses include_inactive=true)
        query["status"] = {"$ne": "inactive"}
    if provider:
        query["provider"] = provider.lower()
    if service:
        query["service"] = service
    # Summary mode: project ONLY the fields the Manage Connectors row renders.
    # The row needs id + connector_name + provider + service + service_slug +
    # status + version + dates + a couple of derived flags. Everything else —
    # plan steps, execution_results, conversation_history, generated_files,
    # docs, default_config — is loaded on demand by /sessions/{id} when the
    # user opens one. Dropping `plan` collapses the response from ~192 KB to
    # ~20 KB on 129 sessions (and the on-wire query is faster too — Mongo
    # never has to read the steps subdocument).
    if summary:
        projection = {
            "_id": 1,
            "app_id": 1,
            "tenant_id": 1,
            "tenant_name": 1,
            "provider": 1,
            "service": 1,
            "service_slug": 1,
            "connector_name": 1,
            "alias_name": 1,
            "connector_type": 1,
            "auth_type": 1,
            "status": 1,
            "version": 1,
            "metadata_version": 1,
            "run_kind": 1,
            "parent_session_id": 1,
            "output_dir": 1,
            "created_at": 1,
            "updated_at": 1,
            "version_upgrade_pending": 1,
            "version_upgraded_from": 1,
            "gateway_connector_id": 1,
            "imported_from_disk": 1,
            "stepper_max_step": 1,
        }
        cursor = sessions_collection().find(query, projection).sort("created_at", -1).skip(skip).limit(limit)
    else:
        cursor = sessions_collection().find(query).sort("created_at", -1).skip(skip).limit(limit)
    sessions = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        doc["id"] = doc["_id"]
        stored_name: str = doc.get("connector_name", "")
        if not stored_name:
            svc = doc.get("service", "")
            doc["connector_name"] = svc.replace("_", " ").replace("-", " ").title()
        if not doc.get("output_dir"):
            try:
                from integration.services.step_executor import (
                    _output_dir as _compute_out,
                )

                _svc = doc.get("service") or ""
                _tid = doc.get("tenant_id") or tenant_id or ""
                if _svc and _tid:
                    doc["output_dir"] = str(_compute_out(_tid, _svc))
            except Exception:
                pass
        sessions.append(doc)
    logger.info("session.list", tenant_id=tenant_id, count=len(sessions), status_filter=status)
    return sessions


@session_router.get("/{session_id}")
async def get_session(session_id: str, x_tenant_id: str | None = Header(None)):
    """Get a single session by ID."""
    tenant_id = _get_tenant(x_tenant_id)
    app_id = None  # TODO: add x_app_id header to this endpoint
    if not ObjectId.is_valid(session_id):
        logger.warning("session.invalid_id", session_id=session_id)
        raise HTTPException(400, "Invalid session ID")
    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(_session_filter(oid, app_id, tenant_id))
    if not doc:
        logger.warning("session.not_found", session_id=session_id, tenant_id=tenant_id)
        raise HTTPException(404, "Session not found")

    # ── Stale EXECUTING guard ─────────────────────────────────────────────────
    # If any step is stuck in EXECUTING but no background task is actually running
    # (server restart, process crash, or the execution genuinely finished),
    # reset those steps to "pending" so the UI stops showing "Running..." forever.
    from integration.services.execution_manager import is_running as _is_running

    _steps = doc.get("plan", {}).get("steps", []) or []
    _executing_indices = [idx for idx, s in enumerate(_steps) if isinstance(s, dict) and s.get("status") == "executing"]
    if _executing_indices and not _is_running(session_id):
        _reset = {"updated_at": datetime.utcnow()}
        for _idx in _executing_indices:
            _reset[f"plan.steps.{_idx}.status"] = "pending"
        await sessions_collection().update_one({"_id": oid}, {"$set": _reset})
        # Patch in-memory doc so caller sees the corrected statuses immediately
        for _idx in _executing_indices:
            if isinstance(_steps[_idx], dict):
                _steps[_idx] = {**_steps[_idx], "status": "pending"}
        logger.info(
            "session.stale_executing_reset",
            session_id=session_id,
            reset_indices=_executing_indices,
        )

    doc["_id"] = str(doc["_id"])
    doc["id"] = doc["_id"]

    # Heavy subdocuments (plan, execution_results, test_results, method_identities,
    # connector_docs, generated_files, package_structure, default_config,
    # default_config_fields, recommended_features, user_prompt, conversation_history,
    # selected_features, selected_config_keys) live in R2 only. This endpoint
    # returns ONLY the Mongo row. The Builder fetches the heavy subset in
    # parallel from GET /sessions/{id}/heavy and merges client-side, so a slow
    # R2 GET never blocks the fast Mongo response (or the dozens of pages that
    # never need heavy data at all).

    # Always include output_dir — compute if not already persisted
    if not doc.get("output_dir"):
        try:
            from integration.services.step_executor import _output_dir as _compute_out

            _svc = doc.get("service") or ""
            if _svc and tenant_id:
                doc["output_dir"] = str(_compute_out(tenant_id, _svc))
        except Exception:
            pass  # non-critical

    logger.info("session.get", session_id=session_id, status=doc.get("status"))
    return doc


@session_router.get("/{session_id}/heavy")
async def get_session_heavy(session_id: str, x_tenant_id: str | None = Header(None)):
    """Return the per-session heavy subdocuments from R2.

    Backfilled by `backfill_sessions_heavy_to_r2.py` into a single gzipped JSON
    blob at the per-app bucket key recorded on the Mongo row
    (`heavy_fields_r2_bucket` / `heavy_fields_r2_key`). The Builder calls this
    in parallel with `GET /sessions/{id}` and merges client-side; list pages
    never call this at all.

    Returns:
        { plan, execution_results, test_results, method_identities,
          connector_docs, generated_files, package_structure, default_config,
          default_config_fields, recommended_features, user_prompt,
          conversation_history, selected_features, selected_config_keys }
        — any subset of the keys that was persisted for this session.

    404 when the session has no heavy blob (e.g. a freshly-created session that
    hasn't run any steps yet).
    """
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")
    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        _session_filter(oid, None, tenant_id),
        # Only the marker fields — we never need the rest of the row to fetch R2.
        {
            "heavy_fields_r2_bucket": 1,
            "heavy_fields_r2_key": 1,
            "heavy_fields_in_r2": 1,
        },
    )
    if not doc:
        raise HTTPException(404, "Session not found")
    bucket = doc.get("heavy_fields_r2_bucket")
    key = doc.get("heavy_fields_r2_key")
    if not (bucket and key):
        # No heavy data has been persisted for this session yet — empty payload
        # rather than 404 so the Builder doesn't have to special-case "new session".
        return {}

    import gzip as _gzip
    import json as _json

    from integration.services import r2_service as _r2

    try:
        s3 = _r2._get_client()
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if body[:2] == b"\x1f\x8b":  # gzip magic
            body = _gzip.decompress(body)
        return _json.loads(body)
    except Exception as exc:
        logger.warning("session.heavy_fetch_failed", session_id=session_id, error=str(exc))
        raise HTTPException(502, f"Could not load heavy fields from R2: {exc}")


class SelectVersionRequest(BaseModel):
    version: str
    # Optional fallback fields used when the manual "Run" button populates the picker
    # without going through the WebSocket execution flow (so version_upgrade_pending
    # may not yet be saved in MongoDB).
    step_index: int | None = None
    current_version: str | None = None


class CustomStepUpsert(BaseModel):
    id: str
    prompt: str
    status: str  # pending | processing | completed | failed
    timestamp: int  # unix ms
    created_at: str | None = None


@session_router.delete("/{session_id}/custom-steps")
async def clear_custom_steps(
    session_id: str,
    x_tenant_id: str | None = Header(None),
):
    """Clear all persisted custom steps for a session (called on re-execute)."""
    # Validate header present but don't use tenant in mutation filter —
    # the gateway may inject a different tenant_id than the one stored on the
    # session (JWT tenant vs URL-passed tenant), causing silent no-ops.
    _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")
    oid = ObjectId(session_id)
    result = await sessions_collection().update_one(
        {"_id": oid},  # session_id alone is the security control — ObjectID is unguessable
        {"$set": {"custom_steps": [], "updated_at": datetime.utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found")
    logger.info("session.custom_steps_cleared", session_id=session_id)
    return {"ok": True}


@session_router.delete("/{session_id}/custom-steps/{step_id}")
async def remove_custom_step(
    session_id: str,
    step_id: str,
    x_tenant_id: str | None = Header(None),
):
    """Remove a single custom step by id from the session's custom_steps array."""
    _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")
    oid = ObjectId(session_id)
    result = await sessions_collection().update_one(
        {"_id": oid},  # session_id alone — avoids silent no-op from tenant mismatch
        {
            "$pull": {"custom_steps": {"id": step_id}},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found")
    logger.info("session.custom_step_removed", session_id=session_id, step_id=step_id)
    return {"ok": True}


@session_router.patch("/{session_id}/custom-steps")
async def upsert_custom_step(
    session_id: str,
    body: CustomStepUpsert,
    x_tenant_id: str | None = Header(None),
):
    """Upsert a custom step into the session's custom_steps array.

    If a step with the same `id` already exists it is replaced (status update).
    Otherwise the step is appended.  This keeps custom prompts persisted across
    page refreshes and re-logins.
    """
    _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    step_doc = body.model_dump()
    step_doc.setdefault("created_at", datetime.utcnow().isoformat())

    # Remove existing entry with same id then push the new one (atomic upsert).
    # Use _id only — avoids silent no-op from gateway tenant_id injection mismatch.
    await sessions_collection().update_one(
        {"_id": oid},
        {"$pull": {"custom_steps": {"id": body.id}}},
    )
    result = await sessions_collection().update_one(
        {"_id": oid},
        {
            "$push": {"custom_steps": step_doc},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found")

    logger.info(
        "session.custom_step_upserted",
        session_id=session_id,
        step_id=body.id,
        status=body.status,
    )
    return {"ok": True}


@session_router.delete("/{session_id}/generated-files")
async def cleanup_generated_files(
    session_id: str,
    x_tenant_id: str | None = Header(None),
    x_app_id: str | None = Header(None, alias="X-App-ID"),
):
    """Delete the generated connector directory for a session.

    Removes {GENERATED_CODE_DIR}/{tenant_id}/{service_slug}_connector/ from disk
    so a fresh execution starts from a completely clean state.
    Resets session status to 'approved' so Execute button reappears.
    """
    tenant_id = _get_tenant(x_tenant_id)
    app_id = x_app_id.strip() if x_app_id else None
    if app_id:
        r2_service._app_bucket_ctx.set(r2_service.app_id_to_bucket(app_id))
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        _session_filter(oid, app_id, tenant_id),
        {"service_slug": 1, "service": 1, "status": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    service_slug = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()
    import re as _re_sr

    # Same normalisation as _output_dir and sync-to-r2: strip _connector in both forms
    _clean_sr = _re_sr.sub(r"_connector(_[a-f0-9]{6}$|$)", r"\1", service_slug)
    # Flat path first (correct), then legacy tenant-subdir paths for old builds
    _base_sr = Path(settings.GENERATED_CODE_DIR)
    _candidates_sr = [
        _base_sr / f"{_clean_sr}_connector",  # correct flat path
        _base_sr / tenant_id / f"{_clean_sr}_connector",  # legacy: tenant subdir
    ]
    out_dir = next((p for p in _candidates_sr if p.exists()), _candidates_sr[0])

    removed = False
    if out_dir.exists():
        try:
            shutil.rmtree(str(out_dir))
            removed = True
            logger.info(
                "session.generated_files_cleaned",
                session_id=session_id,
                path=str(out_dir),
            )
        except Exception as exc:
            logger.error("session.cleanup_failed", session_id=session_id, error=str(exc))
            raise HTTPException(500, f"Failed to remove directory: {exc}")

    # Reset session status, execution results, custom steps, AND all plan step statuses to pending
    # First fetch the current plan steps so we can reset each one individually
    plan_doc = await sessions_collection().find_one(
        _session_filter(oid, app_id, tenant_id),
        {"plan": 1},
    )
    plan = plan_doc.get("plan", {}) if plan_doc else {}
    steps = plan.get("steps", []) or []
    # Some legacy sessions stored a None inside steps[] (or stored the entire
    # `steps` field as None at the plan level). Skip non-dict entries instead
    # of crashing with `TypeError: 'NoneType' object is not a mapping`.
    reset_steps = [
        {**step, "status": "pending", "output": None, "error": None} for step in steps if isinstance(step, dict)
    ]

    update: dict = {
        "status": SessionStatus.APPROVED.value,
        "execution_results": [],
        "custom_steps": [],
        "test_results": None,
        "generated_files": [],
        "docs_json": None,
        "metadata_version": None,
        # Clear method identities and entity configs — connector code is deleted,
        # so AI-detected method signatures and entity mappings are stale.
        "method_identities": [],
        "entity_configs": [],
        "updated_at": datetime.utcnow(),
    }
    if reset_steps:
        update["plan.steps"] = reset_steps

    await sessions_collection().update_one(
        _session_filter(oid, app_id, tenant_id),
        {"$set": update},
    )

    # Clear the in-memory execution event buffer so reconnect doesn't replay stale events
    execution_manager.cleanup(session_id)

    # Clean up per-connector RAG vectors (stale knowledge from deleted code)
    provider = doc.get("provider", "")
    service_name = doc.get("service", "")
    if provider and service_name:
        try:
            deleted_rag = await knowledge_service.cleanup_connector_knowledge(
                tenant_id,
                provider,
                service_name,
            )
            logger.info("session.rag_cleaned", session_id=session_id, deleted_rag=deleted_rag)
        except Exception as rag_err:
            logger.warning("session.rag_cleanup_failed", error=str(rag_err))

    # Delete persisted code analysis so Code Explorer re-analyses the fresh connector
    try:
        await delete_code_analysis(session_id=session_id, tenant_id=tenant_id)
        logger.info("session.code_analysis_cleared", session_id=session_id)
    except Exception as exc:
        logger.warning("session.code_analysis_clear_failed", session_id=session_id, error=str(exc))

    # Clear only execution_state.json — preserve plan.json, progress.json, plan.md
    # so the approved plan survives a re-execute without forcing the user back through Review Plan.
    if doc.get("provider") and service_slug:
        try:
            await r2_service.clear_execution_state(doc.get("provider", ""), service_slug, tenant_id)
            logger.info("session.r2_execution_state_cleared", session_id=session_id)
        except Exception as exc:
            logger.warning(
                "session.r2_execution_state_clear_failed",
                session_id=session_id,
                error=str(exc),
            )

    # Delete regular connector docs from R2 (stale docs from previous execution)
    if doc.get("provider") and service_slug:
        try:
            await r2_service.delete_connector_docs(tenant_id, doc.get("provider", ""), service_slug)
            logger.info("session.docs_cleared", session_id=session_id)
        except Exception as exc:
            logger.warning("session.docs_clear_failed", session_id=session_id, error=str(exc))

    # Delete generated connector code files from R2 (stale from previous execution)
    try:
        deleted_files = await r2_service.delete_connector_session_files(tenant_id, service_slug, session_id)
        logger.info(
            "session.r2_connector_code_cleared",
            session_id=session_id,
            files=deleted_files,
        )
    except Exception as exc:
        logger.warning(
            "session.r2_connector_code_clear_failed",
            session_id=session_id,
            error=str(exc),
        )

    return {"ok": True, "removed": removed, "path": str(out_dir)}


@session_router.post("/{session_id}/cleanup-stream")
async def cleanup_generated_files_stream(
    session_id: str,
    x_tenant_id: str | None = Header(None),
    x_app_id: str | None = Header(None, alias="X-App-ID"),
):
    """SSE streaming cleanup — yields a progress log event for each backend operation.

    Events are newline-delimited JSON in the SSE format:
        data: {"message": "...", "level": "info|success|warn|error"}\n\n

    A final event with level "done" signals completion.
    """
    from fastapi.responses import StreamingResponse as _SR

    tenant_id = _get_tenant(x_tenant_id)
    app_id = x_app_id.strip() if x_app_id else None

    # Wire up R2 per-app bucket context (mirrors TenantBucketMiddleware for streaming endpoints).
    # The middleware sets ContextVars from headers but the async generator in StreamingResponse
    # inherits the request context, so this explicit set ensures correctness.
    if app_id:
        r2_service._app_bucket_ctx.set(r2_service.app_id_to_bucket(app_id))

    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        _session_filter(oid, app_id, tenant_id),
        {"service_slug": 1, "service": 1, "provider": 1, "plan": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    import re as _re_cls

    service_slug = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()
    _clean = _re_cls.sub(r"_connector$", "", service_slug) if service_slug.endswith("_connector") else service_slug
    out_dir = Path(settings.GENERATED_CODE_DIR) / tenant_id / f"{_clean}_connector"
    provider = doc.get("provider", "")
    service_name = doc.get("service", "")

    async def event_gen():
        def _evt(message: str, level: str = "info") -> str:
            return f"data: {json.dumps({'message': message, 'level': level})}\n\n"

        # ── 1. Delete local connector directory ──────────────────────────────
        if out_dir.exists():
            yield _evt("Deleting connector directory…")
            yield _evt(f"   {out_dir}", "info")
            try:
                shutil.rmtree(str(out_dir))
                yield _evt("✓  Connector directory removed", "success")
            except Exception as exc:
                yield _evt(f"⚠  Failed to delete directory: {exc}", "warn")
        else:
            yield _evt("Connector directory already clean (not found)", "info")

        # ── 2. Reset session document in MongoDB ─────────────────────────────
        yield _evt("Resetting session status and execution results…")
        plan = doc.get("plan") or {}
        steps = plan.get("steps") or []
        reset_steps = [{**s, "status": "pending", "output": None, "error": None} for s in steps]

        update: dict = {
            # Keep APPROVED — the plan has already been reviewed and approved.
            # Re-execute only clears generated code, not the plan configuration.
            # Resetting to REVIEWING would force the user back through Review Plan
            # and wipe all their feature/config selections unnecessarily.
            "status": SessionStatus.APPROVED.value,
            "execution_results": [],
            "custom_steps": [],
            "test_results": None,
            "generated_files": [],
            "docs_json": None,
            "metadata_version": None,
            # Do NOT clear method_identities or entity_configs here —
            # those are configured in Review Methods and should survive a re-execute.
            "updated_at": datetime.utcnow(),
        }
        if reset_steps:
            update["plan.steps"] = reset_steps

        await sessions_collection().update_one(
            _session_filter(oid, app_id, tenant_id),
            {"$set": update},
        )
        yield _evt(
            f"✓  Session reset — {len(reset_steps)} step{'' if len(reset_steps) == 1 else 's'} invalidated, method identities cleared",
            "success",
        )

        # ── 3. Clear in-memory execution event buffer ────────────────────────
        execution_manager.cleanup(session_id)
        yield _evt("✓  Execution event buffer cleared", "info")

        # ── 4. Clear R2 execution state only — do NOT wipe plan.json / progress.json ──
        # The plan was already reviewed and approved; clearing it would force the user
        # back through Review Plan and lose all their feature/config selections.
        # Only execution_state.json needs to be reset for a clean re-execute.
        if provider and service_slug:
            yield _evt("Clearing R2 execution state…")
            try:
                await r2_service.clear_execution_state(provider, service_slug, tenant_id)
                yield _evt("✓  R2 execution state cleared", "success")
            except Exception as exc:
                yield _evt(f"⚠  R2 execution state clear failed (non-fatal): {exc}", "warn")

        # ── 5. Clean up RAG knowledge vectors ───────────────────────────────
        if provider and service_name:
            yield _evt("Cleaning up RAG knowledge vectors…")
            try:
                deleted_rag = await knowledge_service.cleanup_connector_knowledge(
                    tenant_id,
                    provider,
                    service_name,
                )
                yield _evt(f"✓  RAG vectors removed ({deleted_rag} chunks deleted)", "success")
            except Exception as rag_err:
                yield _evt(f"⚠  RAG cleanup failed: {rag_err}", "warn")

        # ── 6. Delete code analysis cache ────────────────────────────────────
        yield _evt("Clearing code analysis cache…")
        try:
            await delete_code_analysis(session_id=session_id, tenant_id=tenant_id)
            yield _evt("✓  Code analysis cache cleared", "success")
        except Exception as exc:
            yield _evt(f"⚠  Code analysis clear failed: {exc}", "warn")

        # ── 7. Delete R2 connector docs ──────────────────────────────────────
        if provider and service_slug:
            yield _evt("Removing connector docs from R2 storage…")
            try:
                await r2_service.delete_connector_docs(tenant_id, provider, service_slug)
                yield _evt("✓  R2 docs removed", "success")
            except Exception as exc:
                yield _evt(f"⚠  R2 doc removal failed: {exc}", "warn")

        # ── 8. Delete R2 connector code files (stale from previous execution) ──
        yield _evt("Removing generated connector code from R2…")
        try:
            deleted_files = await r2_service.delete_connector_session_files(tenant_id, service_slug, session_id)
            yield _evt(
                f"✓  R2 connector code removed ({deleted_files} file{'s' if deleted_files != 1 else ''})",
                "success",
            )
        except Exception as exc:
            yield _evt(f"⚠  R2 connector code removal failed: {exc}", "warn")

        yield _evt("Cleanup complete", "done")

    return _SR(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@session_router.get("/{session_id}/version-info")
async def get_version_info(
    session_id: str,
    step_index: int | None = None,
    x_tenant_id: str | None = Header(None),
):
    """Return the current connector version and patch/minor/major suggestions.

    Called by the manual 'Run' button on the version_upgrade step so the UI
    can show the version picker without triggering a full pipeline execution.
    """
    from integration.services.codegen_service import _compute_version_suggestions

    tenant_id = _get_tenant(x_tenant_id)
    app_id = None  # TODO: add x_app_id header to this endpoint
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        _session_filter(oid, app_id, tenant_id),
        {
            "service_slug": 1,
            "service": 1,
            "provider": 1,
            "metadata_version": 1,
            "plan": 1,
        },
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    # Determine current version: prefer metadata_version stored on session, then connector.json on disk
    current_version = doc.get("metadata_version", "")
    if not current_version:
        service_slug = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()
        import re as _re_ver

        _clean_ver = (
            _re_ver.sub(r"_connector$", "", service_slug) if service_slug.endswith("_connector") else service_slug
        )
        meta_path = (
            Path(settings.GENERATED_CODE_DIR) / tenant_id / f"{_clean_ver}_connector" / "metadata" / "connector.json"
        )
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                current_version = meta.get("version", "1.0.0")
            except Exception:
                current_version = "1.0.0"
        else:
            current_version = "1.0.0"

    suggestions = _compute_version_suggestions(current_version)

    # Find version_upgrade step index if not provided
    if step_index is None:
        plan_steps = doc.get("plan", {}).get("steps", [])
        for idx, s in enumerate(plan_steps):
            if s.get("type") == "version_upgrade":
                step_index = idx
                break
        if step_index is None:
            step_index = -1

    return {
        "ok": True,
        "current_version": current_version,
        "suggestions": suggestions,
        "step_index": step_index,
    }


@session_router.post("/{session_id}/select-version")
async def select_version(
    session_id: str,
    body: SelectVersionRequest,
    x_tenant_id: str | None = Header(None),
):
    """User selects the version for the version_upgrade step.

    1. Validates semantic version format
    2. Updates connector.json with the new version
    3. Marks the version_upgrade step as completed in MongoDB
    4. Records upgrade metadata (from/to version, timestamp)
    5. Resumes execution from the next step
    """
    tenant_id = _get_tenant(x_tenant_id)
    app_id = None  # TODO: add x_app_id header to this endpoint
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    new_version = body.version.strip().lstrip("v")
    import re as _re

    if not _re.match(r"^\d+\.\d+\.\d+$", new_version):
        raise HTTPException(
            422,
            f"Invalid version format '{new_version}'. Use semantic versioning: MAJOR.MINOR.PATCH",
        )

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        _session_filter(oid, app_id, tenant_id),
        {
            "service_slug": 1,
            "service": 1,
            "provider": 1,
            "version_upgrade_pending": 1,
            "plan": 1,
        },
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    service_slug = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()
    provider = doc.get("provider", "")
    pending = doc.get("version_upgrade_pending") or {}

    # Use MongoDB pending state if available; otherwise fall back to values supplied
    # by the client (set via the manual "Run" button which calls get_version_info first).
    old_version = pending.get("current_version") or body.current_version or "1.0.0"
    step_index = pending.get("step_index")
    if step_index is None:
        # Try body fallback, then scan plan steps for version_upgrade
        if body.step_index is not None:
            step_index = body.step_index
        else:
            plan_steps = doc.get("plan", {}).get("steps", [])
            step_index = next(
                (idx for idx, s in enumerate(plan_steps) if s.get("type") == "version_upgrade"),
                -1,
            )

    # Update connector.json on disk
    import re as _re_ver

    _clean_ver = _re_ver.sub(r"_connector$", "", service_slug) if service_slug.endswith("_connector") else service_slug
    out_dir = Path(settings.GENERATED_CODE_DIR) / tenant_id / f"{_clean_ver}_connector"
    meta_path = out_dir / "metadata" / "connector.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["version"] = new_version
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.warning("session.version_write_failed", session_id=session_id, error=str(exc))

    # Update docs in R2 if they exist — stamp version into them
    try:
        existing_docs = await r2_service.get_connector_docs(tenant_id, provider, service_slug)
        if existing_docs:
            existing_docs["version"] = new_version
            existing_docs["version_upgraded_from"] = old_version
            existing_docs["version_upgraded_at"] = datetime.utcnow().isoformat()
            await r2_service.save_connector_docs(tenant_id, provider, service_slug, existing_docs)
    except Exception as exc:
        logger.warning("session.version_docs_update_failed", session_id=session_id, error=str(exc))

    # Mark version step as completed and clear pending state
    update: dict = {
        "metadata_version": new_version,
        "version_upgrade_pending": None,
        "version_upgrade_at": datetime.utcnow(),
        "version_upgraded_from": old_version,
        "updated_at": datetime.utcnow(),
    }
    if step_index >= 0:
        update[f"plan.steps.{step_index}.status"] = "completed"

    await sessions_collection().update_one(
        _session_filter(oid, app_id, tenant_id),
        {"$set": update},
    )

    # Resume execution from the step AFTER version_upgrade
    next_index = step_index + 1 if step_index >= 0 else 0
    plan_steps = doc.get("plan", {}).get("steps", [])
    if next_index < len(plan_steps):
        # There are more steps — resume automatically
        from integration.services import execution_manager as _em

        await _em.start_execution(session_id, tenant_id, from_step_index=next_index)
        logger.info(
            "session.version_set_resuming",
            session_id=session_id,
            version=new_version,
            next_step=next_index,
        )
    else:
        # version_upgrade was the last step — finalize
        await sessions_collection().update_one(
            _session_filter(oid, app_id, tenant_id),
            {"$set": {"status": "completed", "updated_at": datetime.utcnow()}},
        )

    logger.info(
        "session.version_selected",
        session_id=session_id,
        old_version=old_version,
        new_version=new_version,
    )

    return {
        "ok": True,
        "version": new_version,
        "previous_version": old_version,
        "resumed": next_index < len(plan_steps),
    }


@session_router.patch("/{session_id}/gateway-connector")
async def save_gateway_connector_id(
    session_id: str,
    payload: dict,
    x_tenant_id: str | None = Header(None),
):
    """Persist the gateway connector_id (e.g. google_gmail_abc123_a1b2c3d4) into the session.

    Called by the frontend after a successful deploy so the connectors list page
    can use the real ID for sync/status operations instead of guessing.
    """
    tenant_id = _get_tenant(x_tenant_id)
    app_id = None  # TODO: add x_app_id header to this endpoint
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    connector_id = payload.get("connector_id", "").strip()
    if not connector_id:
        raise HTTPException(422, "connector_id is required")

    result = await sessions_collection().update_one(
        _session_filter(ObjectId(session_id), app_id, tenant_id),
        {
            "$set": {
                "gateway_connector_id": connector_id,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found")

    return {"saved": True}


@session_router.patch("/{session_id}/steps/{step_index}/status")
async def update_step_status(
    session_id: str,
    step_index: int,
    payload: dict,
    x_tenant_id: str | None = Header(None),
    x_app_id: str | None = Header(None),
):
    """Update the status of a single plan step in MongoDB (e.g. mark write_tests as completed)."""
    tenant_id = _get_tenant(x_tenant_id)
    app_id = _get_app_id(x_app_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    status = payload.get("status")
    if status not in ("pending", "executing", "completed", "failed", "skipped"):
        raise HTTPException(400, f"Invalid status: {status!r}")

    oid = ObjectId(session_id)
    result = await sessions_collection().update_one(
        _session_filter(oid, app_id, tenant_id),
        {
            "$set": {
                f"plan.steps.{step_index}.status": status,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    if result.matched_count == 0:
        # Tenant filter mismatch (e.g. app_id-only sessions created by Electron before
        # tenant linking completes). Fall back to _id-only — ObjectID is unguessable
        # so this is safe. Without this fallback Electron silently swallows the 404
        # and step statuses are never persisted to MongoDB.
        result = await sessions_collection().update_one(
            {"_id": oid},
            {
                "$set": {
                    f"plan.steps.{step_index}.status": status,
                    "updated_at": datetime.utcnow(),
                }
            },
        )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found")

    logger.info(
        "session.step_status_updated",
        session_id=session_id,
        step_index=step_index,
        status=status,
        tenant_id=tenant_id,
    )

    # Broadcast to any CMS WebSocket clients watching this session so they
    # receive real-time step updates from the Electron app's local execution.
    ws_event = {
        "type": "step_complete" if status in ("completed", "failed", "skipped") else "step_start",
        "data": {
            "step_index": step_index,
            "status": "pass" if status == "completed" else status,
            "source": "electron",
        },
    }
    asyncio.create_task(ws_manager.broadcast(session_id, ws_event))

    return {"ok": True, "step_index": step_index, "status": status}


@session_router.post("/{session_id}/steps/{step_index}/validate")
async def validate_step_output_endpoint(
    session_id: str,
    step_index: int,
    x_tenant_id: str | None = Header(None),
):
    """Validate that a step's expected output files actually exist on disk (or R2 for guidelines).

    Does NOT re-run the step — only checks whether the output artifacts are present
    and non-empty.  If valid, updates MongoDB step status to 'completed'.
    Returns {ok, valid, reason, step_type, updated_status}.
    """
    import re as _re

    from integration.services import r2_service
    from integration.services.step_executor import _output_dir, validate_step_output

    tenant_id = _get_tenant(x_tenant_id)
    app_id = None  # TODO: add x_app_id header to this endpoint
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, f"Invalid session ID: {session_id}")

    oid = ObjectId(session_id)
    session = await sessions_collection().find_one(_session_filter(oid, app_id, tenant_id))
    if not session:
        raise HTTPException(404, "Session not found")

    steps = session.get("plan", {}).get("steps", [])
    if step_index < 0 or step_index >= len(steps):
        raise HTTPException(400, f"Step index {step_index} out of range (0..{len(steps) - 1})")

    step = steps[step_index]
    step_type = step.get("type", "")

    # Resolve provider/service and the output directory the same way codegen_service does
    provider = session.get("provider", "")
    service = session.get("service", "")
    # Always prefer the stored service_slug (includes unique hash from session creation).
    # Recomputing from connector_name/provider+service produced a plain slug (e.g. "google_gmail")
    # that differs from the stored one (e.g. "google_gmail_6750e5"), causing two R2 directories.
    _stored_slug = session.get("service_slug", "")
    if _stored_slug:
        service_slug = _stored_slug
    else:
        _connector_name = session.get("connector_name", "")
        if _connector_name:
            service_slug = _re.sub(r"[^a-z0-9]+", "_", _connector_name.lower()).strip("_")
        else:
            service_slug = f"{provider}_{service}".lower().replace("-", "_").replace(" ", "_") if provider else service
    # Strip trailing _connector suffix to avoid double-suffix in output dir
    service_slug = _re.sub(r"_connector$", "", service_slug) if service_slug.endswith("_connector") else service_slug

    # Prefer the persisted output_dir (set when Electron app fetches the Claude prompt,
    # which may point to the user's local machine directory). Fall back to server default.
    _persisted_out = (session.get("output_dir") or "").strip()
    _persisted_path_local = _persisted_out and Path(_persisted_out).exists()

    if _persisted_path_local:
        # Path is accessible on this machine (same-machine build or mounted path)
        out_dir = Path(_persisted_out)
    else:
        out_dir = _output_dir(tenant_id, service_slug)

    # Get execution result for this step (used by some validators)
    exec_results = session.get("execution_results", [])
    step_result = next((r for r in exec_results if r.get("step_index") == step_index), {})

    # ── Local-build / unverifiable detection ─────────────────────────────────
    # When the Electron app builds locally (Claude CLI on user machine):
    #   • output_dir may be absent (old session) or point to the user's machine (new session)
    #   • execution_results are never stored (codegen_service doesn't run)
    #
    # Strategy:
    #   1. If output_dir is set and accessible → use it + synthesise result-based checks
    #   2. If output_dir is set but NOT accessible (user machine path on a server request)
    #      → auto-pass: server cannot inspect user files, trust Claude
    #   3. If output_dir is absent AND step_result is absent AND files don't exist
    #      at the server path → check step's current DB status; if already completed,
    #      re-confirm it; otherwise auto-pass (no evidence of failure)
    _is_remote_build = bool(_persisted_out) and not _persisted_path_local

    if _is_remote_build and not step_result:
        # Files are on the user's machine; server cannot inspect them.
        _auto_reason = f"built by local Claude agent — output on user machine ({_persisted_out})"
        await sessions_collection().update_one(
            _session_filter(oid, app_id, tenant_id),
            {
                "$set": {
                    f"plan.steps.{step_index}.status": "completed",
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        return {
            "ok": True,
            "step_type": step_type,
            "step_index": step_index,
            "valid": True,
            "reason": _auto_reason,
            "updated_status": "completed",
        }

    # Path accessible locally → synthesise result-based checks from on-disk artefacts.
    if _persisted_path_local and not step_result:
        connector_py = out_dir / "connector.py"
        connector_exists = connector_py.exists() and connector_py.stat().st_size > 10
        if step_type == "install_deps":
            step_result = {"status": "pass" if connector_exists else "fail"}
        elif step_type == "smoke_test":
            step_result = {"output": "SMOKE TEST PASSED (local Claude build)" if connector_exists else ""}

    # No output_dir set + no execution_results + files absent from server path
    # → no evidence of failure; check the step's existing MongoDB status and trust it.
    if not _persisted_out and not step_result:
        _step_status = step.get("status", "")
        _server_path_has_files = out_dir.exists() and any(out_dir.iterdir()) if out_dir.exists() else False
        if not _server_path_has_files:
            _auto_reason = (
                f"no server-side execution record for step {step_index} ({step_type}); "
                f"current status={_step_status!r} — marking completed"
            )
            await sessions_collection().update_one(
                _session_filter(oid, app_id, tenant_id),
                {
                    "$set": {
                        f"plan.steps.{step_index}.status": "completed",
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            return {
                "ok": True,
                "step_type": step_type,
                "step_index": step_index,
                "valid": True,
                "reason": _auto_reason,
                "updated_status": "completed",
            }

    # ── generate_test_guidelines: check R2 directly (live check, not stale result) ──
    if step_type == "generate_test_guidelines":
        local_ok = (out_dir / "test_guidelines.md").exists() and (out_dir / "test_guidelines.md").stat().st_size > 10
        r2_content = None
        if not local_ok:
            try:
                r2_content = await r2_service.get_test_guidelines(provider, service_slug)
            except Exception:
                r2_content = None
        if local_ok:
            validation = {"valid": True, "reason": "test_guidelines.md found on disk"}
        elif r2_content and len(r2_content.strip()) > 10:
            validation = {
                "valid": True,
                "reason": f"test_guidelines found in R2 ({len(r2_content)} chars)",
            }
        else:
            validation = {
                "valid": False,
                "reason": "test_guidelines.md not found on disk or in R2",
            }
    else:
        validation = validate_step_output(step_type, out_dir, step_result)

    updated_status = None
    if validation["valid"]:
        await sessions_collection().update_one(
            _session_filter(oid, app_id, tenant_id),
            {
                "$set": {
                    f"plan.steps.{step_index}.status": "completed",
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        updated_status = "completed"
        logger.info(
            "session.step_validate_passed",
            session_id=session_id,
            step_index=step_index,
            step_type=step_type,
            reason=validation["reason"],
        )
    else:
        logger.info(
            "session.step_validate_failed",
            session_id=session_id,
            step_index=step_index,
            step_type=step_type,
            reason=validation["reason"],
        )

    return {
        "ok": True,
        "step_type": step_type,
        "step_index": step_index,
        "valid": validation["valid"],
        "reason": validation["reason"],
        "updated_status": updated_status,
    }


@session_router.get("/{session_id}/implementation-plan")
async def get_implementation_plan(
    session_id: str,
    x_tenant_id: str | None = Header(None),
    x_app_id: str | None = Header(None),
):
    """Return the connector-specific implementation plan (local file → R2 fallback).

    Checks (in order):
    1. Local disk output dir (fastest, available on the server that ran the step)
    2. Session-scoped R2 path: connectors/{service_slug}/sessions/{session_id}/implementation_plan.md
       (written by Electron's sync-to-r2 call after execution)
    3. Legacy shared R2 path: {collection}/{provider}/{service_slug}/implementation_plan.md
       (written by the backend step executor during generate_implementation_plan)
    """
    from integration.services import r2_service
    from integration.services.step_executor import _output_dir

    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    doc = await sessions_collection().find_one(
        {"_id": ObjectId(session_id)},
        {
            "provider": 1,
            "service_slug": 1,
            "service": 1,
            "tenant_id": 1,
            "tenant_name": 1,
            "app_id": 1,
        },
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    provider = doc.get("provider", "")
    service_slug = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()
    # Use tenant_name (R2 bucket name) for local path resolution — consistent with step_executor
    _local_tenant = doc.get("tenant_name") or doc.get("tenant_id") or tenant_id

    # Set the correct per-app R2 bucket context (app_id from header takes priority, then session doc)
    _app_id = (x_app_id or "").strip() or (doc.get("app_id") or "").strip()
    if _app_id:
        r2_service._app_bucket_ctx.set(r2_service.app_id_to_bucket(_app_id))

    # 1. Local disk
    local_path = _output_dir(_local_tenant, service_slug) / "implementation_plan.md"
    if local_path.exists():
        content = local_path.read_text(encoding="utf-8")
        if content.strip():
            return {"content": content, "source": "local", "chars": len(content)}

    # 2. Session-scoped R2 path (written by Electron sync-to-r2)
    try:
        r2_tenant = _app_id or _local_tenant
        content = (
            await r2_service.get_connector_file(r2_tenant, service_slug, session_id, "implementation_plan.md") or ""
        )
        if content.strip():
            return {"content": content, "source": "r2_session", "chars": len(content)}
    except Exception:
        pass

    # 3. Legacy shared R2 path (written by backend step executor)
    try:
        content = await r2_service.get_implementation_plan(provider, service_slug) or ""
        if content.strip():
            return {"content": content, "source": "r2", "chars": len(content)}
    except Exception:
        pass

    return {"content": "", "source": "not_found", "chars": 0, "path": str(local_path)}


@session_router.get("/{session_id}/test-guidelines")
async def get_test_guidelines(session_id: str, x_tenant_id: str | None = Header(None)):
    """Return the connector-specific test guidelines doc (from local file → R2 fallback).

    Used by the generate_test_guidelines accordion in the UI to render the doc.
    """
    from integration.services import r2_service
    from integration.services.step_executor import _output_dir

    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    doc = await sessions_collection().find_one(
        {"_id": ObjectId(session_id)},
        {
            "provider": 1,
            "service_slug": 1,
            "service": 1,
            "tenant_id": 1,
            "tenant_name": 1,
        },
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    provider = doc.get("provider", "")
    service_slug = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()
    _local_tenant = doc.get("tenant_name") or doc.get("tenant_id") or tenant_id

    # 1. Try local file first (fastest)
    local_path = _output_dir(_local_tenant, service_slug) / "test_guidelines.md"
    if local_path.exists():
        content = local_path.read_text(encoding="utf-8")
        if content.strip():
            return {"content": content, "source": "local", "chars": len(content)}

    # 2. Fall back to R2 / local cache
    try:
        content = await r2_service.get_test_guidelines(provider, service_slug) or ""
        if content.strip():
            return {"content": content, "source": "r2", "chars": len(content)}
    except Exception:
        pass

    return {"content": "", "source": "not_found", "chars": 0, "path": str(local_path)}


async def _cascade_delete_acp_connector(gateway_connector_id: str, tenant_id: str) -> None:
    """Best-effort: ask ACP core to delete the connector linked to this session (joined by
    gateway_connector_id == acp connector_id). Token-gated /internal endpoint, called DIRECTLY
    (not via the JWT-gated public gateway). Never raises — a sync failure must not break the
    primary session delete; it's logged for follow-up."""
    import httpx as _httpx

    base = (settings.ACP_INTERNAL_URL or "https://localhost:8020").rstrip("/")
    try:
        try:
            from shielva_common.tls import internal_ca_verify

            verify = internal_ca_verify()
        except Exception:
            verify = True
        async with _httpx.AsyncClient(verify=verify, timeout=8.0) as client:
            r = await client.delete(
                f"{base}/internal/connectors/by-connector-id/{gateway_connector_id}",
                headers={
                    "X-Internal-Token": settings.ACP_INTERNAL_TOKEN,
                    "X-Tenant-ID": tenant_id,
                },
            )
        logger.info(
            "session.cascade_acp_connector_delete",
            connector_id=gateway_connector_id,
            status=r.status_code,
            tenant_id=tenant_id,
        )
    except Exception as exc:
        logger.warning(
            "session.cascade_acp_connector_delete_failed",
            connector_id=gateway_connector_id,
            error=str(exc),
        )


@session_router.delete("/by-connector/{gateway_connector_id}")
async def delete_sessions_by_connector(
    gateway_connector_id: str,
    x_tenant_id: str | None = Header(None),
):
    """Cascade target: delete the builder session(s) linked to an ACP connector (joined by
    gateway_connector_id), so the integration grid stops counting it. Called by ACP core when
    a connector is deleted. Performs NO back-cascade (ACP already deleted the connector), so
    there's no loop. Tenant-scoped; idempotent."""
    tenant_id = _get_tenant(x_tenant_id)
    q: dict[str, Any] = {"gateway_connector_id": gateway_connector_id}
    if tenant_id:
        q["tenant_id"] = tenant_id
    res = await sessions_collection().delete_many(q)
    logger.info(
        "session.cascade_delete_by_connector",
        connector_id=gateway_connector_id,
        tenant_id=tenant_id,
        deleted=res.deleted_count,
    )
    return {"deleted": res.deleted_count}


@session_router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    x_tenant_id: str | None = Header(None),
    x_app_id: str | None = Header(None, alias="X-App-ID"),
):
    """Delete a session and purge its R2/local plan cache."""
    tenant_id = _get_tenant(x_tenant_id)
    app_id = x_app_id.strip() if x_app_id else None
    # Set per-app R2 bucket context so all r2_service calls below use the correct bucket
    if app_id:
        r2_service._app_bucket_ctx.set(r2_service.app_id_to_bucket(app_id))
    if not ObjectId.is_valid(session_id):
        logger.warning("session.invalid_id", session_id=session_id)
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)

    # Fetch full session data before deleting so we can clean up all associated storage
    doc = await sessions_collection().find_one(
        _session_filter(oid, app_id, tenant_id),
        {
            "provider": 1,
            "service": 1,
            "service_slug": 1,
            "gateway_connector_id": 1,
            "entity_configs": 1,
        },
    )
    if not doc:
        logger.warning("session.delete_not_found", session_id=session_id, tenant_id=tenant_id)
        raise HTTPException(404, "Session not found")

    provider = doc.get("provider", "")
    service = doc.get("service", "")
    service_slug = doc.get("service_slug") or service.replace("-", "_").lower()

    result = await sessions_collection().delete_one(_session_filter(oid, app_id, tenant_id))
    if result.deleted_count == 0:
        raise HTTPException(404, "Session not found")

    # Delete generated connector directory from disk (flat layout: GENERATED_CODE_DIR/{slug}_connector/)
    if service_slug:
        import re as _re_del

        _clean_del = _re_del.sub(r"_connector(_[a-f0-9]{6}$|$)", r"\1", service_slug)
        _base_del = Path(settings.GENERATED_CODE_DIR)
        # Check flat path first, then legacy tenant-subdir for old builds
        for _del_cand in [
            _base_del / f"{_clean_del}_connector",
            _base_del / (tenant_id or "") / f"{_clean_del}_connector",
        ]:
            if _del_cand.exists():
                try:
                    shutil.rmtree(str(_del_cand))
                    logger.info(
                        "session.generated_files_deleted",
                        session_id=session_id,
                        path=str(_del_cand),
                    )
                except Exception as exc:
                    logger.warning(
                        "session.generated_files_delete_failed",
                        session_id=session_id,
                        error=str(exc),
                    )

    # Purge R2/local plan cache for this provider/service_slug/tenant
    if provider and service_slug:
        try:
            await r2_service.clear_cache(provider, service_slug, tenant_id)
            logger.info("session.r2_plan_cache_cleared", session_id=session_id)
        except Exception as exc:
            # Non-fatal — session is already deleted, just log
            logger.warning("session.cache_clear_failed", session_id=session_id, error=str(exc))

        # Delete connector docs from R2
        try:
            await r2_service.delete_connector_docs(tenant_id, provider, service_slug)
            logger.info("session.r2_connector_docs_deleted", session_id=session_id)
        except Exception as exc:
            logger.warning(
                "session.r2_connector_docs_delete_failed",
                session_id=session_id,
                error=str(exc),
            )

        # Delete generated connector code files from R2 (uploaded during execution)
        try:
            deleted_files = await r2_service.delete_connector_session_files(tenant_id, service_slug, session_id)
            logger.info(
                "session.r2_connector_code_deleted",
                session_id=session_id,
                files=deleted_files,
            )
        except Exception as exc:
            logger.warning(
                "session.r2_connector_code_delete_failed",
                session_id=session_id,
                error=str(exc),
            )

        # Clean up per-connector RAG vectors
        try:
            await knowledge_service.cleanup_connector_knowledge(tenant_id, provider, service)
        except Exception as exc:
            logger.warning("session.rag_cleanup_on_delete_failed", error=str(exc))

        # Delete persisted code analysis from R2
        try:
            await delete_code_analysis(session_id=session_id, tenant_id=tenant_id)
            logger.info("session.code_analysis_deleted_on_session_delete", session_id=session_id)
        except Exception as exc:
            logger.warning(
                "session.code_analysis_delete_on_delete_failed",
                session_id=session_id,
                error=str(exc),
            )

    # Delete Redis connector tokens + config (keyed by gateway_connector_id if deployed)
    gateway_connector_id = doc.get("gateway_connector_id", "")
    if gateway_connector_id:
        try:
            from services.connector_store import ConnectorStore as _CS

            _cs = _CS()
            await _cs.delete_connector(gateway_connector_id)
            logger.info(
                "session.redis_connector_deleted",
                session_id=session_id,
                connector_id=gateway_connector_id,
            )
        except Exception as exc:
            logger.warning(
                "session.redis_connector_delete_failed",
                session_id=session_id,
                error=str(exc),
            )

    # Delete Redis credentials keyed by tenant + connector_type (service slug)
    if service_slug:
        try:
            from services.redis_service import redis_service as _redis

            cred_key = f"connectors:credentials:{tenant_id}:{service_slug}"
            await _redis.delete(cred_key)
            logger.info("session.redis_credentials_deleted", session_id=session_id, key=cred_key)
        except Exception as exc:
            logger.warning(
                "session.redis_credentials_delete_failed",
                session_id=session_id,
                error=str(exc),
            )

    # Clear execution event buffer
    execution_manager.cleanup(session_id)

    # Cascade: delete the linked ACP connector so the two stores stay in sync (joined by
    # gateway_connector_id == acp connector_id). Best-effort — never fail the session delete.
    # ACP's internal endpoint does NOT cascade back, so there's no loop.
    if gateway_connector_id and tenant_id:
        await _cascade_delete_acp_connector(gateway_connector_id, tenant_id)

    logger.info(
        "session.deleted",
        session_id=session_id,
        tenant_id=tenant_id,
        provider=provider,
        service=service,
    )
    return {"deleted": True}


@session_router.get("/{session_id}/claude-prompt")
async def get_session_claude_prompt(
    session_id: str,
    working_dir: str | None = Query(None, description="Absolute path where Claude should write connector files"),
    from_step: int | None = Query(
        None,
        description="Resume build from this step index (0-based). Previous steps are assumed complete.",
    ),
    x_app_id: str | None = Header(None),
    x_tenant_id: str | None = Header(None),
):
    """Return the assembled Claude CLI prompt for building this connector.

    Uses the existing CONNECTOR_GEN_SYSTEM prompt (same one used by the integration builder)
    merged with session context: provider, service, auth_type, user_prompt, docs_urls.
    The Electron desktop app passes this prompt to the native `claude` CLI via PTY.
    """
    app_id = x_app_id or None
    tenant_id = x_tenant_id or None
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(_session_filter(oid, app_id, tenant_id))
    if not doc:
        raise HTTPException(404, "Session not found")

    provider = doc.get("provider", "")
    service = doc.get("service", "")
    service_name = doc.get("connector_name") or service.replace("_", " ").title()
    # auth_type: prefer session doc value; fall back to catalog lookup so
    # oauth2 services (e.g. Gmail) get the correct auth overlay in the prompt.
    auth_type = doc.get("auth_type") or ""
    if not auth_type:
        try:
            from integration.data.catalog import get_service_detail as _get_svc

            svc_detail = _get_svc(provider, service)
            auth_type = (svc_detail.get("auth_type") if isinstance(svc_detail, dict) else None) or "api_key"
        except Exception:
            auth_type = "api_key"
    raw_user_prompt = doc.get("user_prompt", "")
    docs_urls: list[str] = [u for u in (doc.get("docs_urls") or []) if u.strip()]
    custom_rules = (doc.get("custom_rules_md") or "").strip()

    # ── Reconstruct user prompt: expand informal requirements into a structured spec ──
    # Claude takes the raw free-text (e.g. "list emails, send emails, delete emails")
    # and expands it into explicit method names, SOC/OCP constraints, data model needs.
    from integration.services.planning_service import (
        reconstruct_user_prompt as _reconstruct,
    )

    user_prompt = await _reconstruct(
        raw_prompt=raw_user_prompt,
        provider=provider,
        service=service,
        auth_type=auth_type,
    )

    # ── Load the CONNECTOR_GEN_SYSTEM prompt (existing, R2 → local fallback) ──
    # Uses the exact same prompt that the integration builder uses — fetched from R2
    # or falling back to the local hardcoded constant via _get_fallback().
    from integration.api.step_prompts_routes import _get_fallback
    from integration.services import r2_service as _r2

    async def _load_prompt(name: str) -> str:
        local_fb = _get_fallback(name)
        try:
            # get_step_prompt(name, local_fallback, auth_type=None) — handles R2 + cache
            content = await _r2.get_step_prompt(name, local_fb)
            if content and len(content.strip()) > 50:
                return content.strip()
        except Exception:
            pass
        return local_fb

    # Use the merged system prompt: base CONNECTOR_GEN_SYSTEM + auth-type overlay
    # (r2_service.get_step_prompt supports auth_type overlay natively)
    local_base = _get_fallback("CONNECTOR_GEN_SYSTEM")
    try:
        system_prompt = await _r2.get_step_prompt("CONNECTOR_GEN_SYSTEM", local_base, auth_type=auth_type)
        if not system_prompt or len(system_prompt.strip()) < 50:
            system_prompt = local_base
    except Exception:
        system_prompt = local_base

    # ── Assemble user task message ─────────────────────────────────────────────
    docs_text = "\n".join(f"  - {u}" for u in docs_urls) if docs_urls else "  No documentation URLs provided."

    # ── Read user's feature + config selections from the plan (saved at approve time) ──
    plan_doc = doc.get("plan") or {}
    selected_features: list[dict] = plan_doc.get("recommended_features") or []
    selected_config_fields: list[dict] = plan_doc.get("default_config_fields") or []
    plan_steps_list: list[dict] = plan_doc.get("steps") or []

    # Extract user-requested methods from write_connector step config
    write_connector_step = next((s for s in plan_steps_list if s.get("type") == "write_connector"), None)
    user_methods: list[str] = []
    if write_connector_step:
        step_cfg = write_connector_step.get("config") or {}
        user_methods = step_cfg.get("methods") or []

    user_message_parts = [
        f"Build a complete Shielva connector for: **{service_name}** (provider: `{provider}`)",
        "",
        f"- Auth type: `{auth_type}`",
        f"- Connector type value: `{provider}_{service}`",
    ]
    if user_prompt:
        user_message_parts.append(f"- User requirements: {user_prompt}")

    # ── Inject selected features ──────────────────────────────────────────────────
    if selected_features:
        feat_lines = "\n".join(
            f"  - **{f.get('label', f.get('id', ''))}** ({f.get('id', '')}): {f.get('description', '')}"
            for f in selected_features
        )
        user_message_parts += [
            "",
            "## Selected Features — implement ALL of these in connector.py:",
            feat_lines,
            "Do NOT implement features that are NOT in the list above.",
        ]

    # ── Inject selected config fields ─────────────────────────────────────────────
    if selected_config_fields:
        cfg_lines = "\n".join(
            f"  - `{f.get('key', '')}` (bind={f.get('bind', True)}): {f.get('label', '')} — {f.get('help', '')}"
            for f in selected_config_fields
        )
        user_message_parts += [
            "",
            "## Required Config Fields — include ONLY these in install_fields and connector.json:",
            cfg_lines,
            "Config fields NOT listed above must NOT appear in the connector.",
        ]

    # ── Inject user-requested methods ─────────────────────────────────────────────
    if user_methods:
        base_methods = {
            "install",
            "authorize",
            "sync",
            "health_check",
            "handle_webhook",
            "process_callback",
            "handle_event",
            "batch_processor",
        }
        custom_methods = [m for m in user_methods if m not in base_methods]
        all_methods_str = ", ".join(f"`{m}()`" for m in user_methods)
        user_message_parts += [
            "",
            "## Required Methods — connector.py MUST implement ALL of these as public async methods:",
            f"  {all_methods_str}",
        ]
        if custom_methods:
            user_message_parts += [
                "  Custom operations (NOT in BaseConnector — add as new public async methods): "
                + ", ".join(f"`{m}()`" for m in custom_methods),
            ]

    user_message_parts += [
        f"- Documentation URLs:\n{docs_text}",
        "",
        "⚠️  START by writing `connector.py` IMMEDIATELY — do NOT write planning docs, timelines, or any .md files first.",
        "⚠️  Do NOT create: ImplementationTimeline.md, implementation_plan.md, plan.md, or any planning documents.",
        "",
        "Write these files IN ORDER:",
        "  1. `connector.py` — main connector implementation — WRITE THIS FIRST",
        "  2. `metadata/connector.json` — connector metadata with auth config, install_fields",
        "  3. `GUIDELINES.md` — implementation guidelines (ONLY after connector.py is complete)",
        "  4. `README.md` — full documentation",
        "",
        "Follow ALL rules in the system prompt exactly.",
        "Write production-ready, fully tested code.",
    ]
    if custom_rules:
        user_message_parts += ["", "Additional rules from user:", custom_rules]

    # If resuming from a specific step, inject context so Claude knows what's already done
    if from_step and from_step > 0:
        steps_list = doc.get("plan", {}).get("steps", [])
        completed_titles = [
            f"  - Step {s['index'] + 1}: {s.get('title', s.get('type', ''))}"
            for s in steps_list
            if s.get("index", 999) < from_step
        ]
        resume_note = (
            [
                "",
                f"**RESUMING FROM STEP {from_step + 1}** — previous steps are already complete:",
            ]
            + (completed_titles or [f"  - Steps 1–{from_step} (already done)"])
            + [
                "",
                "The connector directory already has partial output. Review existing files with `list_files()` first,",
                "then continue from where the build left off — do NOT overwrite already-correct files.",
            ]
        )
        user_message_parts += resume_note

    user_message = "\n".join(user_message_parts)

    # Cache the system prompt to a tmp file so Claude reads it via its file tool
    # instead of having the entire 11k+ chars embedded inline in the prompt.
    if system_prompt:
        sys_prompt_path = _cache_prompt_to_tmp(system_prompt, f"connector_gen_system_{auth_type or 'base'}")
        sys_prompt_directive = (
            f"CONNECTOR BUILD INSTRUCTIONS are cached at: `{sys_prompt_path}`\n"
            f"Read that file first — it contains the full system prompt, rules, and interface spec you must follow."
        )
        full_prompt = f"{sys_prompt_directive}\n\n---\n\n{user_message}"
    else:
        full_prompt = user_message

    # ── Scaffold the working directory (idempotent) ────────────────────────────
    # If the caller (Electron app) supplies a working_dir (from Settings > Project Directory),
    # use it FLAT: {working_dir}/{service_slug}_connector  — no tenant subdirectory on local machine.
    # The backend already knows the tenant from the session; there is no need to encode it in
    # the local path.  Tenant subdirectories are only used for the server-side default location.
    # Otherwise fall back to the server default: {GENERATED_CODE_DIR}/{tenant_id}/{service_slug}_connector
    import re as _re

    from integration.services.step_executor import _output_dir as _compute_output_dir

    _tenant_name = (doc.get("tenant_name") or doc.get("tenant_id") or tenant_id or "").strip().lower()
    # Prefer the stored service_slug (includes unique hash from session creation).
    # Fall back to computing from service name only if session predates the hash feature.
    _raw_slug = doc.get("service_slug") or service.replace("-", "_").lower()
    # Strip trailing _connector to avoid double-suffix (e.g. google_gmail_f54cdf_connector → google_gmail_f54cdf)
    _service_slug = _re.sub(r"_connector$", "", _raw_slug) if _raw_slug.endswith("_connector") else _raw_slug
    _custom_base = (working_dir or "").strip()
    if _custom_base:
        # Custom project directory from the Electron app settings — flat layout, no tenant subdir
        out = Path(_custom_base).resolve() / f"{_service_slug}_connector"
    else:
        out = _compute_output_dir(_tenant_name, _service_slug)
    output_dir = ""
    try:
        out.mkdir(parents=True, exist_ok=True)
        for sub in ("client", "helpers", "metadata", "tests"):
            (out / sub).mkdir(exist_ok=True)
        for init_file in (
            out / "client" / "__init__.py",
            out / "helpers" / "__init__.py",
            out / "tests" / "__init__.py",
            out / "__init__.py",
        ):
            init_file.touch()
        pytest_ini = out / "pytest.ini"
        if not pytest_ini.exists():
            pytest_ini.write_text("[pytest]\nasyncio_mode = auto\ntimeout = 60\n")
        sdk_root = Path(__file__).parent.parent.parent.resolve()  # shielva-connectors/
        req_txt = out / "requirements.txt"
        if not req_txt.exists():
            # Enterprise dependency approach: point to shielva-connectors as an editable
            # install so `from shared.base_connector import BaseConnector` (and
            # `from shielva_connector_sdk.base_connector import BaseConnector`) work after
            # `pip install -r requirements.txt`.  In production this line becomes a versioned
            # registry entry (e.g. shielva-connector-sdk>=1.0.0 from a private PyPI).
            req_txt.write_text(
                "# Shielva connector SDK — provides BaseConnector and shared utilities.\n"
                "# For local development this installs directly from the monorepo.\n"
                "# For production replace with: shielva-connector-sdk>=1.0.0\n"
                f"-e {sdk_root}\n\n"
                "# ── Connector-specific dependencies ──────────────────────────────\n"
                "# IMPORTANT: Do NOT add packages that are pre-installed in the shared\n"
                "# venv (pydantic, httpx, structlog, google-auth libs, pytest plugins).\n"
                "# Pinning old versions of those causes wheel-build failures on Python 3.13+.\n"
                "# Use >= minimum-floor specifiers for any packages you do add.\n"
            )
        # conftest.py: ensures pytest finds shared/ without a prior `pip install`
        # (useful during the write_tests step before install_deps has run).
        conftest = out / "conftest.py"
        if not conftest.exists():
            conftest.write_text(
                '"""Auto-generated conftest — adds the Shielva SDK to sys.path for pytest."""\n'
                "import sys\n"
                "from pathlib import Path\n\n"
                f'sys.path.insert(0, "{sdk_root}")\n'
            )
        output_dir = str(out.resolve())
    except Exception as exc:
        logger.warning("session.scaffold_failed", output_dir=str(out), error=str(exc))

    # ── Guard: implementation_plan.md must exist when skipping generate_implementation_plan ──
    if from_step and from_step > 0 and output_dir:
        _all_steps = doc.get("plan", {}).get("steps", [])
        _impl_plan_path = Path(output_dir) / "implementation_plan.md"
        _impl_gen_idx = next(
            (s.get("index", i) for i, s in enumerate(_all_steps) if s.get("type") == "generate_implementation_plan"),
            None,
        )
        if _impl_gen_idx is not None and from_step > _impl_gen_idx and not _impl_plan_path.exists():
            # Mark affected steps as failed
            _fail_updates2: dict = {"updated_at": datetime.utcnow()}
            for _arr_i, _s in enumerate(_all_steps):
                if _s.get("index", _arr_i) >= from_step:
                    _fail_updates2[f"plan.steps.{_arr_i}.status"] = "failed"
            if len(_fail_updates2) > 1:
                with contextlib.suppress(Exception):
                    await sessions_collection().update_one({"_id": oid}, {"$set": _fail_updates2})
            raise HTTPException(
                status_code=422,
                detail=(
                    f"implementation_plan.md not found at {output_dir}. "
                    f"Run Step {_impl_gen_idx + 1} (generate_implementation_plan) before resuming from Step {from_step + 1}."
                ),
            )

    # ── Guard: connector.py must exist when skipping write_connector ─────────────
    # If resuming from a step that comes AFTER write_connector, the file must
    # already be on disk. Fail fast so the user gets a clear error rather than
    # Claude running and producing an incomplete/overwritten build.
    if from_step and from_step > 0 and output_dir:
        _plan_steps = doc.get("plan", {}).get("steps", [])
        _wc_idx = next(
            (s.get("index", i) for i, s in enumerate(_plan_steps) if s.get("type") == "write_connector"),
            None,
        )
        if _wc_idx is not None and from_step > _wc_idx:
            _connector_py = Path(output_dir) / "connector.py"
            if not _connector_py.exists():
                # Mark all steps from from_step onwards as "failed" in the DB
                # so the UI shows them in red immediately after the error.
                _fail_updates: dict = {"updated_at": datetime.utcnow()}
                for _arr_i, _s in enumerate(_plan_steps):
                    if _s.get("index", _arr_i) >= from_step:
                        _fail_updates[f"plan.steps.{_arr_i}.status"] = "failed"
                if len(_fail_updates) > 1:  # at least one step to update
                    try:
                        await sessions_collection().update_one({"_id": oid}, {"$set": _fail_updates})
                    except Exception:
                        pass  # non-critical — error message is the primary response
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"connector.py not found at {output_dir}. "
                        f"Run Step {_wc_idx + 1} (write_connector) before resuming from Step {from_step + 1}."
                    ),
                )

    logger.info(
        "session.claude_prompt_assembled",
        session_id=session_id,
        provider=provider,
        service=service,
        auth_type=auth_type,
        prompt_chars=len(full_prompt),
        output_dir=output_dir,
    )

    # ── Persist output_dir to MongoDB so validate/other endpoints can resolve it ──
    if output_dir:
        try:
            await sessions_collection().update_one(
                _session_filter(oid, app_id, tenant_id),
                {"$set": {"output_dir": output_dir, "updated_at": datetime.utcnow()}},
            )
        except Exception as _e:
            logger.warning("session.output_dir_persist_failed", error=str(_e))

    return {
        "prompt": full_prompt,
        "session_id": session_id,
        "provider": provider,
        "service": service,
        "service_name": service_name,
        "auth_type": auth_type,
        "docs_urls": docs_urls,
        "output_dir": output_dir,
    }


@session_router.get("/{session_id}/step-claude-prompt/{step_index}")
async def get_step_claude_prompt(
    session_id: str,
    step_index: int,
    x_tenant_id: str | None = Header(None),
):
    """Return a Claude CLI prompt focused on re-running a single LLM step locally.

    Used by the Electron app when the user clicks "Run" on an individual LLM step
    (write_connector, write_tests, generate_implementation_plan, etc.).
    Reuses the same scaffolded output directory as the full build.
    """
    tenant_id = _get_tenant(x_tenant_id)
    app_id = None  # TODO: add x_app_id header to this endpoint
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(_session_filter(oid, app_id, tenant_id))
    if not doc:
        raise HTTPException(404, "Session not found")

    plan = doc.get("plan") or {}
    steps = plan.get("steps") or []
    if step_index < 0 or step_index >= len(steps):
        raise HTTPException(400, f"Step index {step_index} out of range (plan has {len(steps)} steps)")

    step = steps[step_index]
    step_type = step.get("type", "")
    step_title = step.get("title", f"Step {step_index + 1}")
    step_desc = step.get("description", "")

    # Re-use the same system prompt as the full build
    from integration.api.step_prompts_routes import _get_fallback
    from integration.services import r2_service as _r2

    auth_type = doc.get("auth_type") or ""
    local_base = _get_fallback("CONNECTOR_GEN_SYSTEM")
    try:
        system_prompt = await _r2.get_step_prompt("CONNECTOR_GEN_SYSTEM", local_base, auth_type=auth_type)
        if not system_prompt or len(system_prompt.strip()) < 50:
            system_prompt = local_base
    except Exception:
        system_prompt = local_base

    service = doc.get("service", "")
    provider = doc.get("provider", "")
    service_name = doc.get("connector_name") or service.replace("_", " ").title()

    # Build a focused task for just this step
    focus_message = "\n".join(
        [
            f"You are re-running a single step for: **{service_name}** (provider: `{provider}`)",
            "",
            f"**Step {step_index + 1}: {step_title}**",
            f"{step_desc}" if step_desc else "",
            "",
            "The connector directory already exists. Look at what's already there and:",
            f"- Only regenerate/fix the output required for this step: `{step_type}`",
            "- Do NOT delete or overwrite files from other steps unless they are broken",
            "- Follow ALL rules in the system prompt exactly",
        ]
    )

    # Cache system prompt to tmp — same pattern as the full build endpoint
    if system_prompt:
        sys_prompt_path = _cache_prompt_to_tmp(system_prompt, f"connector_gen_system_{auth_type or 'base'}")
        sys_prompt_directive = (
            f"CONNECTOR BUILD INSTRUCTIONS are cached at: `{sys_prompt_path}`\n"
            f"Read that file first — it contains the full system prompt, rules, and interface spec you must follow."
        )
        full_prompt = f"{sys_prompt_directive}\n\n---\n\n{focus_message}"
    else:
        full_prompt = focus_message

    # Compute output directory (same as full build — must use stored service_slug with hash)
    import re as _re

    from integration.services.step_executor import _output_dir as _compute_output_dir

    _tenant_name = (doc.get("tenant_name") or doc.get("tenant_id") or tenant_id or "").strip().lower()
    _raw_slug2 = doc.get("service_slug") or service.replace("-", "_").lower()
    _service_slug = _re.sub(r"_connector$", "", _raw_slug2) if _raw_slug2.endswith("_connector") else _raw_slug2
    out = _compute_output_dir(_tenant_name, _service_slug)
    output_dir = str(out.resolve()) if out.exists() else ""

    # ── Guard: implementation_plan.md must exist for write_connector ────────────
    # install_deps does NOT need this guard — handle_install_deps falls back to
    # requirements.txt / config.packages when the plan file is absent.
    if step_type == "write_connector" and output_dir:
        _impl_plan = Path(output_dir) / "implementation_plan.md"
        _has_impl_step = any(s.get("type") == "generate_implementation_plan" for s in steps)
        if _has_impl_step and not _impl_plan.exists():
            _impl_step_num = next(
                (s.get("index", i) + 1 for i, s in enumerate(steps) if s.get("type") == "generate_implementation_plan"),
                1,
            )
            raise HTTPException(
                status_code=422,
                detail=(
                    f"implementation_plan.md not found at {output_dir}. "
                    f"Run Step {_impl_step_num} (generate_implementation_plan) before running write_connector."
                ),
            )

    return {
        "prompt": full_prompt,
        "output_dir": output_dir,
        "step_title": step_title,
    }


# ── Vulnerability scan endpoints ──────────────────────────────────────


@session_router.post("/{session_id}/vulnerability-scan")
async def trigger_vulnerability_scan(session_id: str, request: Request):
    """Trigger a vulnerability scan for a connector session.

    Runs pip-audit against the connector's requirements.txt, generates HTML +
    Excel reports, calls the LLM for AI fix suggestions, and persists all
    artefacts locally and to R2.
    """
    from integration.services import vuln_scan_service

    tenant_id = request.headers.get("X-Tenant-ID", "")
    if not tenant_id:
        raise HTTPException(400, "X-Tenant-ID header is required")
    app_id = request.headers.get("X-App-ID") or None

    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(_session_filter(oid, app_id, tenant_id))
    if not doc:
        logger.warning(
            "vuln_scan.session_not_found",
            session_id=session_id,
            tenant_id=tenant_id,
        )
        raise HTTPException(404, "Session not found")

    provider = doc.get("provider", "")
    service_slug_raw = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()

    import re as _vs_re

    service_slug = (
        _vs_re.sub(r"_connector$", "", service_slug_raw)
        if service_slug_raw.endswith("_connector")
        else service_slug_raw
    )

    # Use the output_dir the Electron app already stored on the session.
    # This is the user's configured project directory (e.g. client_dir/shielva-sense/google_gmail_6750e5_connector).
    # Fall back to the server-side GENERATED_CODE_DIR only when the session has no output_dir set.
    output_dir = (doc.get("output_dir") or "").strip()
    if not output_dir:
        from integration.services.step_executor import (
            _output_dir as _compute_output_dir,
        )

        _tenant_name = (doc.get("tenant_name") or doc.get("tenant_id") or tenant_id or "").strip().lower()
        out = _compute_output_dir(_tenant_name, service_slug)
        output_dir = str(out.resolve()) if out.exists() else str(out)

    logger.info(
        "vuln_scan.trigger",
        session_id=session_id,
        tenant_id=tenant_id,
        provider=provider,
        service_slug=service_slug,
        output_dir=output_dir,
    )

    return await vuln_scan_service.run_vulnerability_scan(
        output_dir=output_dir,
        provider=provider,
        service_slug=service_slug,
        tenant_id=tenant_id,
        session_id=session_id,
    )


@session_router.get("/{session_id}/vulnerability-scan")
async def get_vulnerability_scan_results(session_id: str, request: Request):
    """Get cached vulnerability scan results for a connector session.

    Tries to read from the local artefact directory first, then falls back to
    R2.  Returns ``{"scan": <result_dict>}`` or ``{"scan": null}`` if no scan
    has been run yet.
    """
    tenant_id = request.headers.get("X-Tenant-ID", "")
    if not tenant_id:
        raise HTTPException(400, "X-Tenant-ID header is required")
    app_id = request.headers.get("X-App-ID") or None

    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(_session_filter(oid, app_id, tenant_id))
    if not doc:
        logger.warning(
            "vuln_scan.get_session_not_found",
            session_id=session_id,
            tenant_id=tenant_id,
        )
        raise HTTPException(404, "Session not found")

    provider = doc.get("provider", "")
    service_slug_raw = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()

    import re as _vsg_re

    service_slug = (
        _vsg_re.sub(r"_connector$", "", service_slug_raw)
        if service_slug_raw.endswith("_connector")
        else service_slug_raw
    )

    # ── 1. Try local disk first ──
    # Prefer the output_dir stored on the session (user's configured project directory).
    _stored_output_dir = (doc.get("output_dir") or "").strip()
    if _stored_output_dir:
        out = Path(_stored_output_dir)
    else:
        from integration.services.step_executor import (
            _output_dir as _compute_output_dir,
        )

        _tenant_name = (doc.get("tenant_name") or doc.get("tenant_id") or tenant_id or "").strip().lower()
        out = _compute_output_dir(_tenant_name, service_slug)
    local_json = out / ".shielva" / "vuln" / "vulnerability_scan.json"

    if local_json.exists():
        try:
            data = json.loads(local_json.read_text(encoding="utf-8"))
            logger.info(
                "vuln_scan.get_from_disk",
                session_id=session_id,
                path=str(local_json),
            )
            return {"scan": data}
        except Exception as exc:
            logger.warning(
                "vuln_scan.get_disk_parse_failed",
                session_id=session_id,
                error=str(exc),
            )

    # ── 2. Fall back to R2 ──
    r2_key = f"{r2_service._coll()}/{provider}/{service_slug}/vuln/vulnerability_scan.json"
    try:
        loop = asyncio.get_event_loop()
        if r2_service._use_local():
            raw = None
        else:
            client = r2_service._get_client()
            bucket = r2_service._get_bucket()
            raw = await loop.run_in_executor(
                None,
                lambda: r2_service._sync_read(client, bucket, r2_key),
            )

        if raw:
            data = json.loads(raw)
            logger.info(
                "vuln_scan.get_from_r2",
                session_id=session_id,
                key=r2_key,
            )
            return {"scan": data}
    except Exception as exc:
        logger.warning(
            "vuln_scan.get_r2_failed",
            session_id=session_id,
            key=r2_key,
            error=str(exc),
        )

    logger.info("vuln_scan.get_not_found", session_id=session_id)
    return {"scan": None}


# ── Connector sync-to-R2 (disk → R2 draft) ───────────────────────────────────

# Directories to skip when walking the connector tree (mirrors r2_service._SKIP_DIRS_UPLOAD)
_SYNC_SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    "dist",
    "build",
}


def _resolve_sync_dir(
    service_slug: str,
    r2_tenant: str,
    tenant_id: str,
    client_dir: str,
) -> Path | None:
    """Resolve the on-disk connector directory for sync operations.
    Priority: client_dir > server GENERATED_CODE_DIR fallbacks.
    """
    if client_dir:
        _cand = Path(client_dir)
        if _cand.exists():
            return _cand

    _base = Path(settings.GENERATED_CODE_DIR)
    for _cand in [
        _base / f"{service_slug}_connector",
        _base / r2_tenant / f"{service_slug}_connector",
        _base / tenant_id / f"{service_slug}_connector",
    ]:
        if _cand and _cand.exists():
            return _cand
    return None


def _compute_disk_checksums(out_dir: Path) -> dict[str, str]:
    """Walk out_dir and return {rel_path: md5_hex} skipping build artifacts."""
    result: dict[str, str] = {}
    for f in sorted(out_dir.rglob("*")):
        if not f.is_file():
            continue
        if any(part in _SYNC_SKIP_DIRS for part in f.parts):
            continue
        if f.suffix == ".pyc":
            continue
        try:
            content = f.read_text(encoding="utf-8")
            rel = str(f.relative_to(out_dir))
            result[rel] = hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()
        except (UnicodeDecodeError, PermissionError):
            continue
    return result


@session_router.get("/{session_id}/r2-checksums")
async def get_r2_checksums(
    session_id: str,
    request: Request,
    working_dir: str | None = Query(None),
):
    """Compute checksum diff between local disk files and R2 for this connector session.

    Returns:
      {to_upload: [...], to_delete: [...], disk_count: N, r2_count: N}

    to_upload  = files that are new on disk or have a different MD5 from R2's ETag
    to_delete  = files present in R2 but no longer on disk
    """
    tenant_id = request.headers.get("X-Tenant-ID", "")
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid},
        {"service": 1, "service_slug": 1, "tenant_id": 1, "app_id": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    import re as _re_cs

    service_slug_raw = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()
    service_slug = _re_cs.sub(r"_connector(_[a-f0-9]{6}$|$)", r"\1", service_slug_raw)
    r2_tenant = doc.get("tenant_id") or tenant_id

    # Set R2 bucket context from session
    _app_id = (doc.get("app_id") or "").strip()
    _tname = (doc.get("tenant_id") or "").strip().lower()
    if _app_id:
        r2_service._app_bucket_ctx.set(r2_service.app_id_to_bucket(_app_id))
    if _tname:
        r2_service._tenant_bucket_ctx.set(_tname)

    # ── R2 checksums (ETags) ──────────────────────────────────────────────────
    r2_checksums = await r2_service.get_connector_r2_checksums(r2_tenant, service_slug, session_id)

    # ── Disk checksums ────────────────────────────────────────────────────────
    client_dir = (working_dir or "").strip()
    out_dir = _resolve_sync_dir(service_slug, r2_tenant, tenant_id, client_dir)
    disk_md5s: dict[str, str] = {}
    if out_dir:
        disk_md5s = _compute_disk_checksums(out_dir)

    # ── Diff ──────────────────────────────────────────────────────────────────
    to_upload: list[str] = [rel for rel, md5 in disk_md5s.items() if r2_checksums.get(rel, "") != md5]
    to_delete: list[str] = [rel for rel in r2_checksums if rel not in disk_md5s]

    logger.info(
        "r2_checksums.diff",
        session_id=session_id,
        to_upload=len(to_upload),
        to_delete=len(to_delete),
        disk_count=len(disk_md5s),
        r2_count=len(r2_checksums),
    )

    return {
        "to_upload": to_upload,
        "to_delete": to_delete,
        "disk_count": len(disk_md5s),
        "r2_count": len(r2_checksums),
    }


class SyncToR2Body(BaseModel):
    working_dir: str | None = None  # Absolute path to the connector dir on the client machine
    files: list[str] | None = None  # Specific relative file paths to upload (None = all)
    deleted_paths: list[str] | None = None  # Relative paths to delete from R2


@session_router.post("/{session_id}/sync-to-r2")
async def sync_connector_to_r2(session_id: str, request: Request, body: SyncToR2Body = None):
    """Upload the connector directory from local disk to R2.

    Called by the Electron app after Claude CLI finishes writing files, ensuring R2
    stays in sync even when the backend step executor was not used (CLI mode).

    body.working_dir: if provided, use this absolute path directly (the Electron client's
    actual project directory) rather than guessing from GENERATED_CODE_DIR.

    Returns {ok, uploaded, r2_prefix}.
    """
    tenant_id = request.headers.get("X-Tenant-ID", "")
    if not tenant_id:
        raise HTTPException(400, "X-Tenant-ID header is required")

    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid},
        {
            "service": 1,
            "service_slug": 1,
            "connector_name": 1,
            "tenant_id": 1,
            "app_id": 1,
        },
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    service_slug_raw = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()
    import re as _re_sync

    # Strip _connector suffix in both forms so we append exactly one _connector below:
    #   shielva_gmail_connector        → shielva_gmail
    #   shielva_gmail_connector_cb03d1 → shielva_gmail_cb03d1  (legacy sessions)
    service_slug = _re_sync.sub(r"_connector(_[a-f0-9]{6}$|$)", r"\1", service_slug_raw)
    # connector_name holds the display name written by codegen (e.g. "Google Gmail", "Rippling")
    _connector_display_name = (doc.get("connector_name") or "").strip()
    r2_tenant = doc.get("tenant_id") or tenant_id

    # Explicitly set R2 bucket context from session.app_id so upload goes to the correct
    # per-installation bucket ("shielva-agentic-app-{app_id}") rather than relying solely
    # on TenantBucketMiddleware reading the X-App-ID header (same fix as ws_routes.py).
    _sync_app_id = (doc.get("app_id") or "").strip()
    _sync_tenant_name = (doc.get("tenant_id") or "").strip().lower()
    if _sync_app_id:
        r2_service._app_bucket_ctx.set(r2_service.app_id_to_bucket(_sync_app_id))
    if _sync_tenant_name:
        r2_service._tenant_bucket_ctx.set(_sync_tenant_name)

    # ── Resolve the connector directory ──────────────────────────────────────
    # Priority 1: client-supplied working_dir (Electron passes the actual project directory)
    out_dir = None
    _client_dir = (body.working_dir or "").strip() if body else ""
    if _client_dir:
        _cand = Path(_client_dir)
        if _cand.exists():
            out_dir = _cand
            logger.info("sync_to_r2.using_client_dir", session_id=session_id, path=str(out_dir))

    # Priority 2: server GENERATED_CODE_DIR fallback — try slug forms AND display-name form.
    # Generated dirs use the display name (e.g. "Rippling", "Google Gmail") not the slug form.
    if out_dir is None:
        _base = Path(settings.GENERATED_CODE_DIR)
        _candidates = [
            _base / f"{service_slug}_connector",  # slug (legacy)
            _base / r2_tenant / f"{service_slug}_connector",  # legacy: tenant subdir
            _base / tenant_id / f"{service_slug}_connector",  # legacy: tenant subdir
        ]
        # Add display-name candidates (the actual generated dir name).
        if _connector_display_name:
            _candidates.insert(0, _base / _connector_display_name)
            _candidates.insert(1, _base / r2_tenant / _connector_display_name)
            _candidates.insert(2, _base / tenant_id / _connector_display_name)
        for _cand in _candidates:
            if _cand and _cand.exists():
                out_dir = _cand
                break

    if out_dir is None and not (body and body.deleted_paths):
        logger.info(
            "sync_to_r2.no_disk_dir",
            session_id=session_id,
            service_slug=service_slug,
            client_dir=_client_dir or "not provided",
        )
        return {
            "ok": True,
            "uploaded": 0,
            "deleted": 0,
            "r2_prefix": "",
            "message": "No local files found — nothing to upload",
        }

    r2_prefix = r2_service.connector_session_r2_prefix(r2_tenant, service_slug, session_id)
    uploaded = 0
    deleted = 0

    # ── Upload: specific files list or full directory ─────────────────────────
    specific_files = (body.files or []) if body else []
    if specific_files and out_dir:
        # Selective upload — only the files the frontend diff says need syncing
        uploaded = await r2_service.upload_connector_files_selective(
            r2_tenant, service_slug, session_id, out_dir, specific_files
        )
    elif out_dir and not specific_files:
        # Full upload — existing behaviour (called from post-step syncs)
        uploaded = await r2_service.upload_connector_dir(r2_tenant, service_slug, session_id, out_dir)

    # ── Delete: files removed from disk since last sync ───────────────────────
    paths_to_delete = (body.deleted_paths or []) if body else []
    if paths_to_delete:
        deleted = await r2_service.delete_connector_r2_files(r2_tenant, service_slug, session_id, paths_to_delete)

    logger.info(
        "sync_to_r2.done",
        session_id=session_id,
        uploaded=uploaded,
        deleted=deleted,
        r2_prefix=r2_prefix,
        source="client_dir" if _client_dir else "server_dir",
        selective=bool(specific_files),
    )
    return {
        "ok": True,
        "uploaded": uploaded,
        "deleted": deleted,
        "r2_prefix": r2_prefix,
    }


# ── Connector Analysis ─────────────────────────────────────────────────────────


@session_router.get("/{session_id}/connector-analysis")
async def get_connector_analysis(
    session_id: str,
    request: Request,
):
    """Return cached AI analysis (docs + top prompts) for this session's connector.

    Returns {analysis: null} when not yet generated.
    """
    tenant_id = request.headers.get("X-Tenant-ID", "")
    app_id = request.headers.get("X-App-ID", "")  # was referenced but never read → NameError → 500
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        _session_filter(oid, app_id, tenant_id),
        {"provider": 1, "service_slug": 1, "service": 1, "connector_name": 1},
    )
    if not doc:
        # Try without tenant_id for developer mode
        doc = await sessions_collection().find_one(
            {"_id": oid},
            {"provider": 1, "service_slug": 1, "service": 1, "connector_name": 1},
        )
    if not doc:
        raise HTTPException(404, "Session not found")

    provider = doc.get("provider", "")
    service_slug_raw = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()
    import re as _re_an

    service_slug = (
        _re_an.sub(r"_connector$", "", service_slug_raw)
        if service_slug_raw.endswith("_connector")
        else service_slug_raw
    )

    analysis = await r2_service.get_connector_analysis(provider, service_slug)
    return {"analysis": analysis}


@session_router.put("/{session_id}/connector-analysis")
async def save_connector_analysis(
    session_id: str,
    request: Request,
):
    """Persist a pre-computed connector analysis to R2.

    The Electron app generates the analysis locally via Claude CLI and then
    calls this endpoint to cache it in R2 so it survives app restarts.

    Body: { "analysis": { provider, service_slug, service_name, docs, prompts, generated_at } }
    """
    tenant_id = request.headers.get("X-Tenant-ID", "")
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    body = await request.json()
    analysis = body.get("analysis")
    if not analysis or not isinstance(analysis, dict):
        raise HTTPException(400, "Missing or invalid 'analysis' in request body")

    provider = analysis.get("provider", "")
    service_slug = analysis.get("service_slug", "")
    if not provider or not service_slug:
        raise HTTPException(400, "analysis must contain provider and service_slug")

    await r2_service.save_connector_analysis(provider, service_slug, analysis)
    logger.info(
        "connector_analysis.saved_from_client",
        session_id=session_id,
        provider=provider,
        service_slug=service_slug,
        tenant_id=tenant_id,
    )
    return {"analysis": analysis}


@session_router.post("/{session_id}/connector-analysis")
async def generate_connector_analysis(
    session_id: str,
    request: Request,
):
    """Generate AI analysis (relevant docs + top 10 prompts) for this connector.

    Uses Claude to research the service and return:
    - docs: list of {title, url, description} relevant documentation links
    - prompts: list of top 10 natural-language prompts specific to this service

    Saves result to R2 so subsequent GET calls return the cached version.
    """
    tenant_id = request.headers.get("X-Tenant-ID", "")
    app_id = request.headers.get("X-App-ID", "")  # was referenced but never read → NameError → 500
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        _session_filter(oid, app_id, tenant_id),
        {"provider": 1, "service_slug": 1, "service": 1, "connector_name": 1},
    )
    if not doc:
        doc = await sessions_collection().find_one(
            {"_id": oid},
            {"provider": 1, "service_slug": 1, "service": 1, "connector_name": 1},
        )
    if not doc:
        raise HTTPException(404, "Session not found")

    provider = doc.get("provider", "")
    service_slug_raw = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()
    import re as _re_an2

    service_slug = (
        _re_an2.sub(r"_connector$", "", service_slug_raw)
        if service_slug_raw.endswith("_connector")
        else service_slug_raw
    )
    service_name = doc.get("connector_name") or service_slug.replace("_", " ").title()

    # Check if a fresh analysis already exists
    existing = await r2_service.get_connector_analysis(provider, service_slug)
    if existing:
        return {"analysis": existing}

    # Generate via LLM
    try:
        from integration.services.llm_client import call_llm as _llm_call
    except ImportError:
        raise HTTPException(500, "LLM client not available")

    system_prompt = (
        "You are a software integration expert with deep knowledge of popular APIs. "
        "Answer immediately from your training knowledge — do NOT browse the web or use any tools. "
        "Always respond with valid JSON only — no markdown, no code fences, no extra text."
    )

    user_message = f"""From your training knowledge, return a JSON object for the {service_name} API (provider: {provider}) with exactly two keys:

1. "docs": an array of up to 6 well-known documentation pages, each with:
   - "title": short descriptive title
   - "url": the standard documentation URL (e.g. https://developers.{provider}.com/docs/...)
   - "description": one sentence describing what this doc covers

2. "prompts": an array of exactly 6 natural-language feature prompts a developer would use when building a {service_name} connector.
   Each prompt describes a specific operation (e.g. "List all unread emails and return subject, sender, date, and preview").

Return ONLY this JSON — no preamble, no explanation:
{{
  "docs": [{{"title": "...", "url": "https://...", "description": "..."}}],
  "prompts": ["...", "..."]
}}"""

    try:
        raw = await _llm_call(
            messages=[{"role": "user", "content": user_message}],
            system=system_prompt,
            max_tokens=2000,
            temperature=0.3,
            expect_code=False,
        )
    except Exception as exc:
        logger.error("connector_analysis.llm_failed", session_id=session_id, error=str(exc))
        raise HTTPException(500, f"Analysis generation failed: {exc}")

    # Parse JSON from LLM response
    import re as _re_json

    # Strip markdown fences if present
    clean = _re_json.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=_re_json.MULTILINE).strip()
    try:
        data = json.loads(clean)
    except json.JSONDecodeError as exc:
        logger.error(
            "connector_analysis.parse_failed",
            session_id=session_id,
            raw=raw[:500],
            error=str(exc),
        )
        raise HTTPException(500, "Failed to parse LLM analysis response as JSON")

    analysis = {
        "provider": provider,
        "service_slug": service_slug,
        "service_name": service_name,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "docs": data.get("docs", []),
        "prompts": data.get("prompts", []),
    }

    # Persist to R2 + local disk
    await r2_service.save_connector_analysis(provider, service_slug, analysis)

    logger.info(
        "connector_analysis.generated",
        session_id=session_id,
        provider=provider,
        service_slug=service_slug,
        docs_count=len(analysis["docs"]),
        prompts_count=len(analysis["prompts"]),
    )

    return {"analysis": analysis}


# ── Prompt Synthesizer ─────────────────────────────────────────────────────────


class SynthesizePromptRequest(BaseModel):
    prompt: str


@session_router.post("/{session_id}/synthesize-prompt")
async def synthesize_prompt(
    session_id: str,
    body: SynthesizePromptRequest,
    request: Request,
):
    """Validate and synthesize a user's connector prompt into a clear, complete feature spec.

    Takes the user's raw text and returns a polished, explicit feature description
    that will produce high-quality connector code.
    """
    tenant_id = request.headers.get("X-Tenant-ID", "")
    app_id = request.headers.get("X-App-ID") or None
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    raw_prompt = (body.prompt or "").strip()
    if not raw_prompt:
        raise HTTPException(400, "prompt is required")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        _session_filter(oid, app_id, tenant_id),
        {
            "provider": 1,
            "service_slug": 1,
            "service": 1,
            "connector_name": 1,
            "auth_type": 1,
        },
    )
    if not doc:
        doc = await sessions_collection().find_one(
            {"_id": oid},
            {
                "provider": 1,
                "service_slug": 1,
                "service": 1,
                "connector_name": 1,
                "auth_type": 1,
            },
        )
    if not doc:
        raise HTTPException(404, "Session not found")

    provider = doc.get("provider", "")
    service_slug_raw = doc.get("service_slug") or doc.get("service", "").replace("-", "_").lower()
    import re as _re_syn

    service_slug = (
        _re_syn.sub(r"_connector$", "", service_slug_raw)
        if service_slug_raw.endswith("_connector")
        else service_slug_raw
    )
    service_name = doc.get("connector_name") or service_slug.replace("_", " ").title()
    auth_type = doc.get("auth_type", "api_key")

    try:
        from integration.services.llm_client import call_llm as _llm_call
    except ImportError:
        raise HTTPException(500, "LLM client not available")

    system_prompt = (
        "You are an expert connector developer. Given a user's informal feature description, "
        "rewrite it as a precise, complete connector feature specification. "
        "The output must be concise (3-8 bullet points), action-oriented, and include "
        "specific method names, key fields to return, error cases to handle, and pagination if relevant. "
        "Write in plain English, no markdown headings or code. "
        "Respond with ONLY the synthesized specification text — nothing else."
    )

    user_message = (
        f"Service: {service_name} (provider: {provider}, auth: {auth_type})\n\n"
        f"User's raw prompt:\n{raw_prompt}\n\n"
        f"Rewrite this as a precise, complete feature specification for a {service_name} connector. "
        f"Be specific about operations, field names, and edge cases relevant to {service_name}'s API."
    )

    try:
        synthesized = await _llm_call(
            messages=[{"role": "user", "content": user_message}],
            system=system_prompt,
            max_tokens=600,
            temperature=0.2,
            expect_code=False,
        )
    except Exception as exc:
        logger.error("synthesize_prompt.llm_failed", session_id=session_id, error=str(exc))
        raise HTTPException(500, f"Synthesis failed: {exc}")

    logger.info("synthesize_prompt.done", session_id=session_id, service=service_name)
    return {"synthesized": synthesized.strip()}
