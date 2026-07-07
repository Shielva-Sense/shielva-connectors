"""Integration Builder — Entity Builder API routes.

Manages MongoDB entity configurations for methods with 'api_response_persistent' identity.
Handles connection provisioning, collection configuration, field mapping, and AI analysis.
"""

import uuid
from datetime import datetime
from typing import Any

import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from integration.db.database import sessions_collection

logger = structlog.get_logger(__name__)

entity_router = APIRouter(prefix="/entity-builder", tags=["entity-builder"])


def _get_tenant(x_tenant_id: str | None) -> str:
    if not x_tenant_id:
        raise HTTPException(400, "X-Tenant-ID header required")
    return x_tenant_id


# ── Provision / Connection ────────────────────────────────────────────


class ProvisionRequest(BaseModel):
    connection_string: str
    database_name: str


@entity_router.post("/{session_id}/provision")
async def provision_mongo(
    session_id: str,
    payload: ProvisionRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """Save MongoDB connection details to session."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)

    # Read existing provision so we can preserve connection_tested when only
    # the database_name changes (e.g. user picks from dropdown after testing).
    existing = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"mongo_provision": 1},
    )
    if not existing:
        raise HTTPException(404, "Session not found")

    prev = existing.get("mongo_provision") or {}
    prev_conn = prev.get("connection_string", "")
    prev_tested = prev.get("connection_tested", False)

    # If the connection string changed, treat as a fresh provision (reset tested).
    # If only the database changed (same connection string), keep connection_tested.
    connection_changed = prev_conn and prev_conn != payload.connection_string
    new_tested = False if connection_changed else prev_tested

    provision = {
        "connection_string": payload.connection_string,
        "database_name": payload.database_name,
        "connection_tested": new_tested,
        "tested_at": prev.get("tested_at") if not connection_changed else None,
    }

    result = await sessions_collection().update_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"$set": {"mongo_provision": provision, "updated_at": datetime.utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found")

    logger.info("entity.provision_saved", session_id=session_id, db=payload.database_name)
    return {"ok": True, "provision": provision}


@entity_router.post("/{session_id}/test-connection")
async def test_connection(
    session_id: str,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """Test MongoDB connection using provisioned credentials stored in the session.

    No request body needed — connection_string and database_name are read from the
    mongo_provision field saved by the /provision endpoint.
    """
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    # Read provisioned credentials from the session document
    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"mongo_provision": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    provision = doc.get("mongo_provision") or {}
    connection_string = provision.get("connection_string", "")
    provision.get("database_name", "")

    if not connection_string:
        raise HTTPException(422, "No connection string provisioned. Call /provision first.")

    from motor.motor_asyncio import AsyncIOMotorClient

    client = None
    try:
        client = AsyncIOMotorClient(
            connection_string,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
        # Ping to verify
        await client.admin.command("ping")

        # List databases
        db_list = await client.list_database_names()
        # Filter system databases
        db_list = [d for d in db_list if d not in ("admin", "local", "config")]

        # Mark provision as tested
        await sessions_collection().update_one(
            {"_id": oid, "tenant_id": tenant_id},
            {
                "$set": {
                    "mongo_provision.connection_tested": True,
                    "mongo_provision.tested_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }
            },
        )

        logger.info("entity.connection_tested", session_id=session_id, databases=len(db_list))
        return {"ok": True, "databases": db_list, "message": "Connection successful"}

    except Exception as e:
        logger.error("entity.connection_failed", session_id=session_id, error=str(e))
        return {"ok": False, "databases": [], "message": f"Connection failed: {e}"}
    finally:
        if client:
            client.close()


@entity_router.get("/{session_id}/databases")
async def list_databases(
    session_id: str,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """List databases on provisioned MongoDB instance."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"mongo_provision": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    provision = doc.get("mongo_provision")
    if not provision or not provision.get("connection_string"):
        raise HTTPException(422, "No MongoDB connection provisioned for this session")

    from motor.motor_asyncio import AsyncIOMotorClient

    client = None
    try:
        client = AsyncIOMotorClient(
            provision["connection_string"],
            serverSelectionTimeoutMS=5000,
        )
        db_list = await client.list_database_names()
        db_list = [d for d in db_list if d not in ("admin", "local", "config")]
        return {"databases": db_list}
    except Exception as e:
        raise HTTPException(502, f"Failed to list databases: {e}")
    finally:
        if client:
            client.close()


class CollectionsRequest(BaseModel):
    database_name: str


@entity_router.get("/{session_id}/collections")
async def list_collections(
    session_id: str,
    database_name: str,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """List collections in a specific database."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"mongo_provision": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    provision = doc.get("mongo_provision")
    if not provision or not provision.get("connection_string"):
        raise HTTPException(422, "No MongoDB connection provisioned")

    from motor.motor_asyncio import AsyncIOMotorClient

    client = None
    try:
        client = AsyncIOMotorClient(
            provision["connection_string"],
            serverSelectionTimeoutMS=5000,
        )
        db = client[database_name]
        collections = await db.list_collection_names()
        return {"database": database_name, "collections": collections}
    except Exception as e:
        raise HTTPException(502, f"Failed to list collections: {e}")
    finally:
        if client:
            client.close()


# ── Entity CRUD ───────────────────────────────────────────────────────


class CreateEntityRequest(BaseModel):
    collection_name: str
    database_name: str
    connection_string: str = ""
    fields: list[dict[str, Any]] = []


@entity_router.post("/{session_id}/entities")
async def create_entity(
    session_id: str,
    payload: CreateEntityRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """Create a new entity configuration."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    entity = {
        "entity_id": str(uuid.uuid4()),
        "collection_name": payload.collection_name,
        "database_name": payload.database_name,
        "connection_string": payload.connection_string,
        "fields": payload.fields,
        "created_at": datetime.utcnow().isoformat(),
    }

    oid = ObjectId(session_id)
    result = await sessions_collection().update_one(
        {"_id": oid, "tenant_id": tenant_id},
        {
            "$push": {"entity_configs": entity},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found")

    logger.info("entity.created", session_id=session_id, entity_id=entity["entity_id"])
    return {"ok": True, "entity": entity}


@entity_router.put("/{session_id}/entities/{entity_id}")
async def update_entity(
    session_id: str,
    entity_id: str,
    payload: CreateEntityRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """Update an existing entity configuration."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"entity_configs": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    entities = doc.get("entity_configs", [])
    found = False
    for ent in entities:
        if ent.get("entity_id") == entity_id:
            ent["collection_name"] = payload.collection_name
            ent["database_name"] = payload.database_name
            if payload.connection_string:
                ent["connection_string"] = payload.connection_string
            ent["fields"] = payload.fields
            found = True
            break

    if not found:
        raise HTTPException(404, f"Entity {entity_id} not found")

    await sessions_collection().update_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"$set": {"entity_configs": entities, "updated_at": datetime.utcnow()}},
    )

    logger.info("entity.updated", session_id=session_id, entity_id=entity_id)
    return {"ok": True, "entity_id": entity_id}


@entity_router.delete("/{session_id}/entities/{entity_id}")
async def delete_entity(
    session_id: str,
    entity_id: str,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """Delete an entity configuration."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    result = await sessions_collection().update_one(
        {"_id": oid, "tenant_id": tenant_id},
        {
            "$pull": {"entity_configs": {"entity_id": entity_id}},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found")

    logger.info("entity.deleted", session_id=session_id, entity_id=entity_id)
    return {"ok": True, "entity_id": entity_id}


# ── Field Mapping ─────────────────────────────────────────────────────


class FieldMappingRequest(BaseModel):
    method_name: str
    mappings: list[dict[str, Any]]


@entity_router.post("/{session_id}/entities/{entity_id}/field-mappings")
async def save_field_mappings(
    session_id: str,
    entity_id: str,
    payload: FieldMappingRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """Save field mappings for a method-entity pair."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"method_identities": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    identities = doc.get("method_identities", [])
    found = False
    for mi in identities:
        if mi.get("method_name") == payload.method_name:
            mi["entity_id"] = entity_id
            mi["field_mappings"] = payload.mappings
            found = True
            break

    if not found:
        identities.append(
            {
                "method_name": payload.method_name,
                "identity": "api_response_persistent",
                "auto_detected": False,
                "entity_id": entity_id,
                "field_mappings": payload.mappings,
                "expected_response_fields": [],
            }
        )

    await sessions_collection().update_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"$set": {"method_identities": identities, "updated_at": datetime.utcnow()}},
    )

    logger.info(
        "entity.mappings_saved",
        session_id=session_id,
        entity_id=entity_id,
        method=payload.method_name,
    )
    return {"ok": True, "entity_id": entity_id, "mappings": len(payload.mappings)}


# ── Wizard Draft (persist intermediate state between refreshes) ───────


class WizardDraftRequest(BaseModel):
    method_name: str | None = None  # scopes the draft to a specific method
    collection_name: str | None = None
    database_name: str | None = None  # persisted so restore works even if mongo_provision is null
    fields: list[dict[str, Any]] | None = None
    mappings: list[dict[str, Any]] | None = None


@entity_router.patch("/{session_id}/wizard-draft")
async def save_wizard_draft(
    session_id: str,
    payload: WizardDraftRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """Persist intermediate wizard state (collection, fields, mappings) so the user
    doesn't have to re-enter data after a page refresh.
    Draft is scoped per method_name so each method has independent wizard state."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    # Scope draft key to method name to avoid cross-method bleed
    key_prefix = f"wizard_draft.{payload.method_name}" if payload.method_name else "wizard_draft._default"

    updates: dict[str, Any] = {"updated_at": datetime.utcnow()}
    if payload.collection_name is not None:
        updates[f"{key_prefix}.collection_name"] = payload.collection_name
    if payload.database_name is not None:
        updates[f"{key_prefix}.database_name"] = payload.database_name
    if payload.fields is not None:
        updates[f"{key_prefix}.fields"] = payload.fields
    if payload.mappings is not None:
        updates[f"{key_prefix}.mappings"] = payload.mappings

    oid = ObjectId(session_id)
    result = await sessions_collection().update_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"$set": updates},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found")

    return {"ok": True}


# ── Wizard State (restore on re-open) ────────────────────────────────


@entity_router.get("/{session_id}/wizard-state")
async def get_wizard_state(
    session_id: str,
    method_name: str | None = None,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """Return the full saved wizard state for a session+method so the frontend
    can restore all steps without the user re-entering anything."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {
            "mongo_provision": 1,
            "entity_configs": 1,
            "method_identities": 1,
            "wizard_draft": 1,
        },
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    provision = doc.get("mongo_provision") or {}
    # Draft is scoped per method — fall back to legacy top-level draft for old sessions
    all_drafts = doc.get("wizard_draft") or {}
    if method_name and isinstance(all_drafts.get(method_name), dict):
        draft = all_drafts[method_name]
    elif method_name:
        # No per-method draft yet — start fresh (don't inherit other methods' drafts)
        draft = {}
    else:
        draft = all_drafts.get("_default") or {}

    # Find entity config and method identity for this method
    entity_config = None
    method_identity = None

    if method_name:
        for mi in doc.get("method_identities") or []:
            if mi.get("method_name") == method_name:
                method_identity = mi
                break
        if method_identity and method_identity.get("entity_id"):
            for ec in doc.get("entity_configs") or []:
                if ec.get("entity_id") == method_identity["entity_id"]:
                    entity_config = ec
                    break

    # Merge draft into entity_config for intermediate state
    # draft takes precedence over saved entity_config for collection/fields
    draft_collection = draft.get("collection_name") or (entity_config or {}).get("collection_name")
    draft_fields = draft.get("fields") or (entity_config or {}).get("fields") or []
    draft_mappings = draft.get("mappings") or (method_identity or {}).get("field_mappings") or []

    # Determine the highest completed step
    # Step 0: Connection — done if connection tested
    # Step 1: Collection — done if collection_name saved in draft or entity_config
    # Step 2: Fields — done if fields saved in draft or entity_config
    # Step 3: Mappings — done if mappings saved in draft or method_identity
    # Step 4: Review — done if entity fully saved (entity_id exists)
    completed_step = -1
    if provision.get("connection_tested"):
        completed_step = 0
    if draft_collection:
        completed_step = 1
    if draft_fields:
        completed_step = 2
    if draft_mappings:
        completed_step = 3
    if entity_config and entity_config.get("entity_id"):
        completed_step = 4

    # database_name: prefer provisioned value, fall back to what was saved in draft
    resolved_db_name = provision.get("database_name", "") or draft.get("database_name", "")

    return {
        "ok": True,
        "provision": {
            "connection_string": provision.get("connection_string", ""),
            "database_name": resolved_db_name,
            "connection_tested": provision.get("connection_tested", False),
        },
        "entity_config": entity_config,
        "method_identity": method_identity,
        "draft": {
            "collection_name": draft_collection or "",
            "fields": draft_fields,
            "mappings": draft_mappings,
            "database_name": draft.get("database_name", ""),
        },
        "completed_step": completed_step,
    }


# ── AI Analysis ───────────────────────────────────────────────────────


class AnalyzeRequest(BaseModel):
    method_name: str
    method_source: str | None = None  # pre-loaded source from Electron client


@entity_router.post("/{session_id}/analyze-response")
async def analyze_response(
    session_id: str,
    payload: AnalyzeRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """AI analyzes a method to predict response field structure."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    from integration.services.identity_detection_service import predict_response_fields

    method_source = (payload.method_source or "").strip()

    # If the caller (Electron app) already sent the method source, use it directly.
    # Otherwise fall back to reading connector.py from disk (CMS server-side flow).
    if not method_source:
        from pathlib import Path as _Path

        from integration.api.connector_api_routes import _resolve_connector_dir

        # Try the session's persisted output_dir first (Electron builds on user machine)
        oid_check = ObjectId(session_id)
        _sess = await sessions_collection().find_one(
            {"_id": oid_check, "tenant_id": tenant_id},
            {"output_dir": 1},
        )
        _persisted = (_sess or {}).get("output_dir", "")

        if _persisted and _Path(_persisted).exists():
            connector_py = _Path(_persisted) / "connector.py"
        else:
            out_dir = await _resolve_connector_dir(session_id, tenant_id)
            connector_py = out_dir / "connector.py"

        if not connector_py.exists():
            raise HTTPException(404, "connector.py not found — pass method_source in the request body")

        import ast as _ast

        source = connector_py.read_text(encoding="utf-8")
        tree = _ast.parse(source)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                        if item.name == payload.method_name:
                            method_source = _ast.get_source_segment(source, item) or ""
                            break

    if not method_source:
        raise HTTPException(404, f"Method '{payload.method_name}' not found")

    fields = await predict_response_fields(method_source, payload.method_name)

    # Save predicted fields to session
    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"method_identities": 1},
    )
    if doc:
        identities = doc.get("method_identities", [])
        found = False
        for mi in identities:
            if mi.get("method_name") == payload.method_name:
                mi["expected_response_fields"] = fields
                found = True
                break
        if not found:
            identities.append(
                {
                    "method_name": payload.method_name,
                    "identity": "api_response_persistent",
                    "auto_detected": False,
                    "expected_response_fields": fields,
                    "field_mappings": [],
                    "entity_id": None,
                }
            )
        await sessions_collection().update_one(
            {"_id": oid, "tenant_id": tenant_id},
            {
                "$set": {
                    "method_identities": identities,
                    "updated_at": datetime.utcnow(),
                }
            },
        )

    logger.info(
        "entity.response_analyzed",
        session_id=session_id,
        method=payload.method_name,
        fields=len(fields),
    )
    return {"ok": True, "method_name": payload.method_name, "predicted_fields": fields}


@entity_router.post("/{session_id}/apply-persistence/{method_name}")
async def apply_persistence_to_connector(
    session_id: str,
    method_name: str,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """Refactors connector.py to use a BaseRepository subclass for MongoDB persistence.

    Steps:
    1. Generates repository/{service_slug}_repository.py (BaseRepository subclass)
    2. Asks LLM to refactor connector.py to import + use the repository class
    3. Validates syntax and writes updated connector.py
    4. Stores entity builder config to R2 for the implement_persistence plan step
    """
    import ast
    import re as _re

    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {
            "method_identities": 1,
            "entity_configs": 1,
            "mongo_provision": 1,
            "provider": 1,
            "service_slug": 1,
            "connector_name": 1,
        },
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    # Resolve entity config + mappings for this method
    method_identity = next(
        (mi for mi in (doc.get("method_identities") or []) if mi.get("method_name") == method_name),
        None,
    )
    if not method_identity:
        raise HTTPException(404, f"No method identity found for {method_name}")

    entity_id = method_identity.get("entity_id")
    field_mappings = method_identity.get("field_mappings", [])
    entity_config = next(
        (ec for ec in (doc.get("entity_configs") or []) if ec.get("entity_id") == entity_id),
        None,
    )
    if not entity_config:
        raise HTTPException(404, f"Entity config not found for entity_id={entity_id}")

    # Resolve connector.py path
    from integration.api.connector_api_routes import _resolve_connector_dir

    out_dir = await _resolve_connector_dir(session_id, tenant_id)
    connector_py = out_dir / "connector.py"
    if not connector_py.exists():
        raise HTTPException(404, "connector.py not found — generate the connector first")

    source = connector_py.read_text(encoding="utf-8")

    collection_name = entity_config.get("collection_name", "results")
    database_name = entity_config.get("database_name", "connector_data")

    # Derive service_slug and repository class name
    _provider = doc.get("provider", "unknown")
    _service_slug = doc.get("service_slug") or doc.get("connector_name", "unknown")
    # Strip trailing _connector suffix for cleaner naming
    _clean_slug = _re.sub(r"_connector$", "", _service_slug) if _service_slug.endswith("_connector") else _service_slug
    _connector_class = "".join(w.capitalize() for w in _clean_slug.replace("-", "_").split("_"))
    _repo_class = f"{_connector_class}Repository"
    _repo_module = f"{_clean_slug}_repository"

    # ── Step 1: Generate the repository file ──────────────────────────
    repo_dir = out_dir / "repository"
    repo_dir.mkdir(exist_ok=True)
    init_py = repo_dir / "__init__.py"
    if not init_py.exists():
        init_py.write_text("", encoding="utf-8")

    # Build document construction lines from field mappings
    mapping_lines = []
    for fm in field_mappings:
        resp_path = fm.get("response_path", "")
        entity_field = fm.get("entity_field", "")
        transform = fm.get("transform", "")
        if transform:
            mapping_lines.append(f'            "{entity_field}": {transform},')
        else:
            mapping_lines.append(f'            "{entity_field}": response.get("{resp_path}"),')
    mappings_str = (
        "\n".join(mapping_lines)
        if mapping_lines
        else "            # no explicit mappings — persisting full response\n            **response,"
    )

    repo_code = f'''"""Auto-generated repository for {_clean_slug} connector.

Tenant isolation: database = {{tenant_id}}_{database_name}
Generated by Shielva integration builder — do not edit manually.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from shared.repository_service import BaseRepository
from typing import Any, Dict


class {_repo_class}(BaseRepository):
    DATABASE_NAME = "{database_name}"

    async def save_{method_name}_result(self, response: Dict[str, Any]) -> str:
        """Persist {method_name} API response to collection: {collection_name}."""
        document = {{
{mappings_str}
        }}
        return await self.insert_one("{collection_name}", document)
'''

    repo_file = repo_dir / f"{_repo_module}.py"
    repo_file.write_text(repo_code, encoding="utf-8")
    logger.info("entity.repo_generated", session_id=session_id, file=str(repo_file))

    # ── Step 2: LLM refactors connector.py to use the repository ──────
    mapping_desc = (
        "\n".join(
            f"  - response['{fm.get('response_path', '')}'] → '{fm.get('entity_field', '')}'"
            + (f" (transform: {fm.get('transform', '')})" if fm.get("transform") else "")
            for fm in field_mappings
        )
        or "  (no explicit mappings — persist full response dict)"
    )

    prompt = f"""You are refactoring a Python connector class to use a repository class for MongoDB persistence.

REPOSITORY CLASS (already generated at repository/{_repo_module}.py):
    class {_repo_class}(BaseRepository):
        DATABASE_NAME = "{database_name}"
        async def save_{method_name}_result(self, response: dict) -> str: ...

TASK:
1. Add this import near the top of the file (after existing imports):
   from repository.{_repo_module} import {_repo_class}

2. Add a new async helper method `_persist_{method_name}_result(self, response: dict) -> str` to the connector class:
   ```python
   async def _persist_{method_name}_result(self, response: dict) -> str:
       repo = {_repo_class}(
           tenant_id=self.config.get("tenant_id", ""),
           connection_string=self.config.get("mongo_connection_string") or self.config.get("connection_string", ""),
       )
       try:
           return await repo.save_{method_name}_result(response)
       finally:
           await repo.close()
   ```

3. Modify the existing method `{method_name}` to:
   - Call `persisted_id = await self._persist_{method_name}_result(result)` after the API response is obtained.
   - Return the original response dict merged with `{{"_persisted_id": persisted_id}}`.
   - Keep ALL existing logic intact — only insert the persistence call.

Field mappings applied by the repository ({collection_name} collection):
{mapping_desc}

Return the COMPLETE modified connector.py source. No markdown fences. No explanation. Only raw Python."""

    from integration.services.llm_client import call_llm_fix

    try:
        new_source = await call_llm_fix(
            [{"role": "user", "content": prompt}],
            system=source,
            max_tokens=35000,
        )
    except Exception as e:
        raise HTTPException(502, f"LLM call failed: {e}")

    # Strip markdown fences if LLM wrapped the output
    if new_source.startswith("```"):
        lines = new_source.splitlines()
        new_source = "\n".join(
            line
            for i, line in enumerate(lines)
            if not (i == 0 and line.startswith("```")) and not (i == len(lines) - 1 and line.strip() == "```")
        )

    # Validate syntax before writing
    try:
        ast.parse(new_source)
    except SyntaxError as exc:
        raise HTTPException(
            422,
            f"LLM produced code with syntax error: {exc}. Retry or check field mappings.",
        )

    connector_py.write_text(new_source, encoding="utf-8")

    # ── Step 3: Persist entity builder config to R2 ────────────────────
    try:
        from integration.services.r2_service import store_entity_builder_config

        r2_payload = {
            "method_name": method_name,
            "entity_id": entity_id,
            "entity_config": entity_config,
            "field_mappings": field_mappings,
            "collection_name": collection_name,
            "database_name": database_name,
            "repo_class": _repo_class,
            "repo_module": _repo_module,
            "prompt": prompt,
        }
        await store_entity_builder_config(_provider, _service_slug, method_name, r2_payload)
        logger.info(
            "entity.r2_config_saved",
            provider=_provider,
            service_slug=_service_slug,
            method=method_name,
        )
    except Exception as _r2_exc:
        logger.warning("entity.r2_config_save_failed", error=str(_r2_exc))

    logger.info(
        "entity.persistence_applied",
        session_id=session_id,
        method=method_name,
        collection=collection_name,
        database=database_name,
        repo_class=_repo_class,
        repo_file=str(repo_file),
    )
    return {
        "ok": True,
        "method_name": method_name,
        "collection": collection_name,
        "database": database_name,
        "mappings_applied": len(field_mappings),
        "repository_class": _repo_class,
        "repository_file": str(repo_file),
    }


@entity_router.post("/{session_id}/inject-persistence-step")
async def inject_persistence_step(
    session_id: str,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """Inject implement_persistence step into the session plan (before write_tests).

    Called after entity builder wizard completes. Idempotent — no-op if the step
    already exists.
    """
    from datetime import datetime as _dt

    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"plan": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    plan = doc.get("plan") or {}
    steps = plan.get("steps", [])

    # Idempotent check
    if any(s.get("type") == "implement_persistence" for s in steps):
        return {"ok": True, "injected": False, "reason": "Step already exists"}

    # Find insertion point: before write_tests
    write_tests_idx = next(
        (i for i, s in enumerate(steps) if s.get("type") == "write_tests"),
        len(steps),
    )
    new_step = {
        "index": write_tests_idx,
        "type": "implement_persistence",
        "title": "Implement Persistence",
        "description": (
            "Generates tenant-specific repository class and verifies connector.py "
            "has been refactored with persistence logic for all api_response_persistent methods."
        ),
        "estimated_duration_s": 60,
        "config": {},
        "status": "pending",
    }
    steps.insert(write_tests_idx, new_step)
    # Re-index
    for i, s in enumerate(steps):
        s["index"] = i

    await sessions_collection().update_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"$set": {"plan.steps": steps, "updated_at": _dt.utcnow()}},
    )

    logger.info("entity.step_injected", session_id=session_id, at_index=write_tests_idx)
    return {"ok": True, "injected": True, "step_index": write_tests_idx}


class GenerateCodeRequest(BaseModel):
    method_name: str
    entity_id: str


@entity_router.post("/{session_id}/generate-persistence-code")
async def generate_persistence_code(
    session_id: str,
    payload: GenerateCodeRequest,
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
):
    """AI generates persistence code for a method + entity pair."""
    tenant_id = _get_tenant(x_tenant_id)
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"method_identities": 1, "entity_configs": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    # Find entity config
    entity = None
    for ec in doc.get("entity_configs") or []:
        if ec.get("entity_id") == payload.entity_id:
            entity = ec
            break
    if not entity:
        raise HTTPException(404, f"Entity {payload.entity_id} not found")

    # Find method identity + mappings
    method_identity = None
    for mi in doc.get("method_identities") or []:
        if mi.get("method_name") == payload.method_name:
            method_identity = mi
            break

    field_mappings = (method_identity or {}).get("field_mappings", [])

    # Import lazily
    from integration.api.connector_api_routes import _resolve_connector_dir
    from integration.services.identity_detection_service import (
        generate_persistence_code as _gen_code,
    )

    out_dir = await _resolve_connector_dir(session_id, tenant_id)
    connector_py = out_dir / "connector.py"
    if not connector_py.exists():
        raise HTTPException(404, "connector.py not found")

    source = connector_py.read_text(encoding="utf-8")

    # Extract method source
    import ast

    tree = ast.parse(source)
    method_source = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name == payload.method_name:
                        method_source = ast.get_source_segment(source, item) or ""
                        break

    if not method_source:
        raise HTTPException(404, f"Method {payload.method_name} not found")

    code = await _gen_code(method_source, entity, field_mappings)

    logger.info("entity.code_generated", session_id=session_id, method=payload.method_name)
    return {"ok": True, "method_name": payload.method_name, "generated_code": code}
