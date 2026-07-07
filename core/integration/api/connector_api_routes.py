"""Integration Builder — Connector API exploration & execution routes.

Allows users to:
1. List methods (APIs) exposed by a generated connector
2. View method signatures and docstrings
3. Execute methods with parameters (Postman-like)
4. Manage method identity assignments (api_response, void, etc.)
"""

import ast
import asyncio
import contextlib
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from integration.core.config import settings
from integration.db.database import sessions_collection

logger = structlog.get_logger(__name__)

connector_api_router = APIRouter(prefix="/connector-api", tags=["connector-api"])


async def _resolve_connector_dir(
    session_id: str,
    tenant_id: str,
    working_dir: str | None = None,
) -> Path:
    """Find the generated connector directory for a session.

    If working_dir is provided (absolute path sent by the Electron client), use it directly.
    Otherwise falls back to: {GENERATED_CODE_DIR}/{tenant_id}/{service_slug}_connector/
    """
    # ── Client-supplied absolute path (agentic-developer sets this) ────────────
    if working_dir and working_dir.strip():
        out_dir = Path(working_dir.strip())
        if not out_dir.exists():
            raise HTTPException(404, f"connector.py not found in generated files (looked in: {out_dir})")
        return out_dir

    # ── Fallback: derive from MongoDB session + GENERATED_CODE_DIR ─────────────
    try:
        oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(400, "Invalid session ID")

    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"service": 1, "service_slug": 1},
    )
    if not session:
        raise HTTPException(404, "Session not found")

    service_slug = session.get("service_slug") or session.get("service", "").replace("-", "_").lower()
    import re as _re

    _clean = _re.sub(r"_connector$", "", service_slug) if service_slug.endswith("_connector") else service_slug
    out_dir = Path(settings.GENERATED_CODE_DIR) / tenant_id / f"{_clean}_connector"

    if not out_dir.exists():
        raise HTTPException(404, "connector.py not found in generated files")

    return out_dir


# ── Identity detection heuristics ─────────────────────────────────────

_VOID_NAME_PATTERNS = {
    "set_",
    "update_",
    "delete_",
    "remove_",
    "configure_",
    "handle_",
    "process_",
    "initialize_",
    "on_",
}
_DB_PATTERNS = {
    "collection.",
    "insert",
    "save_to",
    "persist",
    "store",
    "upsert",
    "bulk_write",
}


def _detect_identity(func_node: ast.AST) -> str:
    """Detect the behavioral identity of a method from its AST node."""
    ast.dump(func_node)
    name = getattr(func_node, "name", "")

    # Check return annotation
    return_type = ast.unparse(func_node.returns) if getattr(func_node, "returns", None) else None

    # Collect return statements and their values
    has_return_value = False
    has_api_call = False
    has_transformation = False
    has_db_write = False

    for node in ast.walk(func_node):
        # Check for return with value
        if isinstance(node, ast.Return) and node.value is not None:
            has_return_value = True

        # Check for API calls (self.client.*, httpx.*, requests.*)
        if isinstance(node, ast.Attribute):
            attr_str = ""
            with contextlib.suppress(Exception):
                attr_str = ast.unparse(node)
            if any(p in attr_str for p in ["self.client.", "httpx.", "self._api_request"]):
                has_api_call = True
            if any(p in attr_str.lower() for p in _DB_PATTERNS):
                has_db_write = True

        # Check for transformations (list comprehensions, dict comprehensions, for-loops with append)
        if isinstance(node, (ast.ListComp, ast.DictComp, ast.SetComp)):
            has_transformation = True
        if isinstance(node, ast.Call):
            try:
                call_str = ast.unparse(node.func)
                if ".append(" in call_str or ".extend(" in call_str:
                    has_transformation = True
            except Exception:
                pass

    # Check for void-like method names
    if any(name.startswith(p) for p in _VOID_NAME_PATTERNS):
        if not has_return_value:
            return "void"

    # Return type is None explicitly
    if return_type == "None":
        return "void"

    # No return statement with value
    if not has_return_value:
        return "void"

    # Has both API call and DB write — persistent
    if has_api_call and has_db_write:
        return "api_response_persistent"

    # Has API call with transformation
    if has_api_call and has_transformation:
        return "api_response_processed"

    # Has API call — raw response
    if has_api_call:
        return "api_response"

    # Has return value but no clear API call pattern — assume processed
    if has_return_value:
        return "api_response_processed"

    return "api_response"


def _parse_methods(connector_py: Path) -> list[dict[str, Any]]:
    """Parse connector.py with AST to extract public method info + auto-detected identity."""
    source = connector_py.read_text(encoding="utf-8")
    tree = ast.parse(source)

    methods = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = item.name
            # Skip private/dunder methods
            if name.startswith("_"):
                continue

            # Extract parameters (skip 'self')
            params = []
            args = item.args
            defaults_offset = len(args.args) - len(args.defaults)
            for i, arg in enumerate(args.args):
                if arg.arg == "self":
                    continue
                param: dict[str, Any] = {"name": arg.arg}
                # Type annotation
                if arg.annotation:
                    param["type"] = ast.unparse(arg.annotation)
                # Default value
                default_idx = i - defaults_offset
                if default_idx >= 0 and default_idx < len(args.defaults):
                    try:
                        param["default"] = ast.literal_eval(args.defaults[default_idx])
                    except (ValueError, TypeError):
                        param["default"] = ast.unparse(args.defaults[default_idx])
                params.append(param)

            # Keyword-only args
            kw_defaults_map = dict(zip(args.kwonlyargs, args.kw_defaults, strict=False))
            for kw_arg in args.kwonlyargs:
                param_info: dict[str, Any] = {"name": kw_arg.arg}
                if kw_arg.annotation:
                    param_info["type"] = ast.unparse(kw_arg.annotation)
                default_node = kw_defaults_map.get(kw_arg)
                if default_node:
                    try:
                        param_info["default"] = ast.literal_eval(default_node)
                    except (ValueError, TypeError):
                        param_info["default"] = ast.unparse(default_node)
                params.append(param_info)

            # Return type
            return_type = ast.unparse(item.returns) if item.returns else None

            # Docstring
            docstring = ast.get_docstring(item) or ""

            # Method type
            is_async = isinstance(item, ast.AsyncFunctionDef)

            # Categorize method
            category = "custom"
            lifecycle = ["install", "authorize", "sync", "health_check"]
            data_ops = ["fetch_documents", "stream_documents", "normalize"]
            auth_ops = ["get_oauth_url", "test_connection"]
            if name in lifecycle:
                category = "lifecycle"
            elif name in data_ops:
                category = "data"
            elif name in auth_ops:
                category = "auth"

            # HTTP method hint based on name
            http_method = "GET"
            if any(
                kw in name.lower()
                for kw in [
                    "create",
                    "post",
                    "send",
                    "install",
                    "authorize",
                    "store",
                    "write",
                    "add",
                ]
            ):
                http_method = "POST"
            elif any(kw in name.lower() for kw in ["update", "edit", "modify"]):
                http_method = "PUT"
            elif any(kw in name.lower() for kw in ["delete", "remove"]):
                http_method = "DELETE"

            # Auto-detect behavioral identity
            auto_identity = _detect_identity(item)

            methods.append(
                {
                    "name": name,
                    "is_async": is_async,
                    "parameters": params,
                    "return_type": return_type,
                    "docstring": docstring,
                    "category": category,
                    "http_method": http_method,
                    "line_number": item.lineno,
                    "auto_detected_identity": auto_identity,
                    "source": ast.get_source_segment(source, item) or "",
                }
            )

    return methods


@connector_api_router.get("/{session_id}/methods")
async def list_connector_methods(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    working_dir: str | None = None,
):
    """List all public methods of the generated connector.

    working_dir: absolute path to the connector output directory on the client machine.
    When provided by the Electron/agentic-developer client, it overrides the server-side
    GENERATED_CODE_DIR so files written to a custom project directory are found correctly.
    """
    out_dir = await _resolve_connector_dir(session_id, x_tenant_id, working_dir)
    connector_py = out_dir / "connector.py"
    if not connector_py.exists():
        raise HTTPException(404, "connector.py not found in generated files")

    # Run sync CPU-bound AST parse in a thread so it doesn't block the event loop
    loop = asyncio.get_event_loop()
    methods = await loop.run_in_executor(None, _parse_methods, connector_py)

    # Also get session info for context (including saved identities)
    oid = ObjectId(session_id)
    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"provider": 1, "service": 1, "user_prompt": 1, "method_identities": 1},
    )

    # Merge saved identities into method list
    saved_identities = {
        mi["method_name"]: mi
        for mi in (session.get("method_identities") or [])
        if isinstance(mi, dict) and "method_name" in mi
    }
    for m in methods:
        saved = saved_identities.get(m["name"])
        if saved:
            m["identity"] = saved.get("identity", m["auto_detected_identity"])
            m["identity_auto_detected"] = saved.get("auto_detected", True)
            m["entity_id"] = saved.get("entity_id")
        else:
            m["identity"] = m["auto_detected_identity"]
            m["identity_auto_detected"] = True
            m["entity_id"] = None

    logger.info(
        "connector_api.list_methods",
        session_id=session_id,
        method_count=len(methods),
    )

    return {
        "session_id": session_id,
        "provider": session.get("provider") if session else None,
        "service": session.get("service") if session else None,
        "connector_file": str(connector_py.relative_to(out_dir)),
        "methods": methods,
        "method_count": len(methods),
    }


# ── Method Identity CRUD ──────────────────────────────────────────────


@connector_api_router.get("/{session_id}/method-identities")
async def get_method_identities(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Return all saved method identity assignments for this session."""
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"method_identities": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    return {"session_id": session_id, "identities": doc.get("method_identities", [])}


class BulkIdentityRequest(BaseModel):
    identities: list[dict[str, Any]]


@connector_api_router.post("/{session_id}/method-identities")
async def save_method_identities(
    session_id: str,
    payload: BulkIdentityRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Bulk-save method identity assignments to session."""
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    result = await sessions_collection().update_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {
            "$set": {
                "method_identities": payload.identities,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Session not found")

    logger.info(
        "connector_api.save_identities",
        session_id=session_id,
        count=len(payload.identities),
    )
    return {"ok": True, "saved": len(payload.identities)}


class SingleIdentityRequest(BaseModel):
    identity: str
    entity_id: str | None = None
    field_mappings: list[dict[str, Any]] = []


@connector_api_router.put("/{session_id}/method-identities/{method_name}")
async def update_method_identity(
    session_id: str,
    method_name: str,
    payload: SingleIdentityRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Update a single method's identity assignment."""
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"method_identities": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    identities: list = doc.get("method_identities", [])
    found = False
    for mi in identities:
        if mi.get("method_name") == method_name:
            mi["identity"] = payload.identity
            mi["auto_detected"] = False
            mi["entity_id"] = payload.entity_id
            mi["field_mappings"] = payload.field_mappings
            found = True
            break

    if not found:
        identities.append(
            {
                "method_name": method_name,
                "identity": payload.identity,
                "auto_detected": False,
                "entity_id": payload.entity_id,
                "field_mappings": payload.field_mappings,
                "expected_response_fields": [],
            }
        )

    await sessions_collection().update_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {
            "$set": {
                "method_identities": identities,
                "updated_at": datetime.utcnow(),
            }
        },
    )

    logger.info(
        "connector_api.update_identity",
        session_id=session_id,
        method=method_name,
        identity=payload.identity,
    )
    return {"ok": True, "method_name": method_name, "identity": payload.identity}


@connector_api_router.delete("/{session_id}/method-identities/{method_name}/entity")
async def remove_method_entity(
    session_id: str,
    method_name: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Remove the entity linkage from a method (unlink entity, keep identity)."""
    if not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")

    oid = ObjectId(session_id)
    doc = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"method_identities": 1},
    )
    if not doc:
        raise HTTPException(404, "Session not found")

    identities: list = doc.get("method_identities", [])
    for mi in identities:
        if mi.get("method_name") == method_name:
            mi["entity_id"] = None
            mi["field_mappings"] = []
            break

    await sessions_collection().update_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {
            "$set": {
                "method_identities": identities,
                "updated_at": datetime.utcnow(),
            }
        },
    )

    logger.info("connector_api.remove_entity", session_id=session_id, method=method_name)
    return {"ok": True, "method_name": method_name, "entity_id": None}


# ── Source / Config routes ────────────────────────────────────────────


@connector_api_router.get("/{session_id}/source")
async def get_connector_source(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Return the full connector source code for reference."""
    out_dir = await _resolve_connector_dir(session_id, x_tenant_id)
    connector_py = out_dir / "connector.py"
    if not connector_py.exists():
        raise HTTPException(404, "connector.py not found")

    content = connector_py.read_text(encoding="utf-8")
    return {
        "session_id": session_id,
        "path": "connector.py",
        "content": content,
        "size": len(content),
    }


class ExecuteMethodRequest(BaseModel):
    method_name: str
    parameters: dict[str, Any] = {}


@connector_api_router.get("/{session_id}/config")
async def get_connector_config(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Return the connector config if it exists."""
    out_dir = await _resolve_connector_dir(session_id, x_tenant_id)
    config_py = out_dir / "config.py"
    if not config_py.exists():
        return {"session_id": session_id, "config": None}

    content = config_py.read_text(encoding="utf-8")

    # Parse config fields from the settings class
    tree = ast.parse(content)
    fields = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    field: dict[str, Any] = {
                        "name": item.target.id,
                        "type": ast.unparse(item.annotation) if item.annotation else "str",
                    }
                    if item.value:
                        try:
                            field["default"] = ast.literal_eval(item.value)
                        except (ValueError, TypeError):
                            field["default"] = ast.unparse(item.value)
                    fields.append(field)

    return {
        "session_id": session_id,
        "config_source": content,
        "fields": fields,
    }
