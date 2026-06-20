"""Integration Builder — Catalog API routes (static + custom providers)."""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from integration.data.catalog import get_all_providers, get_provider_services, get_service_detail, SERVICE_CATALOG
from integration.db.database import custom_providers_collection, static_provider_overrides_collection
from integration.services import r2_service
from integration.services.llm_client import call_llm_json

logger = structlog.get_logger(__name__)

catalog_router = APIRouter(prefix="/catalog", tags=["catalog"])


# ── Pydantic models ───────────────────────────────────────────────────

class ServiceInput(BaseModel):
    service_key: str
    display_name: str
    description: str
    auth_type: str = "api_key"
    category: str = "general"
    logo_url: Optional[str] = ""


class DependencyInput(BaseModel):
    name: str
    version: Optional[str] = ""
    reason: Optional[str] = ""
    is_custom: bool = False


class CreateCustomProviderRequest(BaseModel):
    display_name: str
    description: str
    website_url: Optional[str] = ""
    brand_color: Optional[str] = "#14B8A6"
    logo_url: Optional[str] = ""
    services: List[ServiceInput] = []
    dependencies: List[DependencyInput] = []


class UpdateCustomProviderRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    website_url: Optional[str] = None
    brand_color: Optional[str] = None
    logo_url: Optional[str] = None
    services: Optional[List[ServiceInput]] = None
    dependencies: Optional[List[DependencyInput]] = None


class SuggestServicesRequest(BaseModel):
    provider_name: str
    description: str
    website_url: Optional[str] = ""


class SuggestDependenciesRequest(BaseModel):
    provider_name: str
    services: List[Dict[str, str]]  # [{"display_name": "...", "auth_type": "..."}]
    custom_prompt: Optional[str] = ""        # Free-form user instruction
    attached_docs: Optional[List[str]] = []  # Markdown file contents provided by the user


class ValidatePackageRequest(BaseModel):
    package_name: str
    provider_name: str


# ── Helpers ───────────────────────────────────────────────────────────

def _serialize(doc: Dict) -> Dict:
    """Convert ObjectId fields to strings."""
    doc = dict(doc)
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    doc["id"] = doc.get("_id", "")
    return doc


def _provider_key(display_name: str) -> str:
    """Derive a slug-style provider key from display name."""
    return display_name.lower().replace(" ", "_").replace("-", "_")


# ── Static catalog endpoints ──────────────────────────────────────────

@catalog_router.get("/providers")
async def list_providers():
    """Return all providers (static catalog + custom + static overrides) with service counts."""
    providers = get_all_providers()
    # Tag static providers
    for p in providers:
        p["is_custom"] = False

    # Merge static provider overrides (super-admin edits of built-in providers)
    try:
        overrides = await static_provider_overrides_collection().find({}).to_list(None)
        override_map: Dict[str, Dict] = {}
        for ov in overrides:
            ov = _serialize(ov)
            override_map[ov.get("provider_key", "")] = ov
        # Apply overrides to matching static providers
        for p in providers:
            ov = override_map.get(p["provider"])
            if ov:
                if ov.get("display_name"):
                    p["display_name"] = ov["display_name"]
                if ov.get("description"):
                    p["description"] = ov["description"]
                if ov.get("logo_url"):
                    p["logo_url"] = ov["logo_url"]
                if ov.get("brand_color"):
                    p["brand_color"] = ov["brand_color"]
                # Merge extra services from override
                extra = ov.get("extra_services", [])
                p["service_count"] = p.get("service_count", 0) + len(extra)
                p["override_id"] = ov["id"]
                p["modified_by"] = ov.get("modified_by", "")
                p["modified_at"] = ov.get("modified_at", "")
    except Exception as exc:
        logger.warning("catalog.static_overrides_fetch_failed", error=str(exc))

    # Merge custom providers from DB
    try:
        customs = await custom_providers_collection().find({}).to_list(None)
        for c in customs:
            c = _serialize(c)
            services = c.get("services", [])
            providers.append({
                "provider": c.get("provider_key", _provider_key(c.get("display_name", ""))),
                "display_name": c.get("display_name", ""),
                "service_count": len(services),
                "categories": list({s.get("category", "general") for s in services}),
                "logo_url": c.get("logo_url", ""),
                "is_custom": True,
                "custom_id": c["id"],
                "brand_color": c.get("brand_color", "#14B8A6"),
                "description": c.get("description", ""),
                "modified_by": c.get("modified_by", ""),
                "modified_at": c.get("modified_at", ""),
            })
    except Exception as exc:
        logger.warning("catalog.custom_providers_fetch_failed", error=str(exc))

    logger.info("catalog.list_providers", count=len(providers))
    return sorted(providers, key=lambda p: p["display_name"].lower())


@catalog_router.get("/providers/{provider}/services")
async def list_services(provider: str):
    """Return all services offered by a provider — static catalog + custom additions merged.

    A provider can have services from both sources:
      1. Static catalog (`SERVICE_CATALOG` in data/catalog.py)
      2. Custom services stored in `custom_providers` MongoDB collection

    Both contribute to the rendered card grid so adding a service for a provider
    that already exists in the static catalog (e.g. an extra Google service like
    Looker) just requires an insert into `custom_providers` — no Python edit.
    """
    services = list(get_provider_services(provider))
    seen_keys = {s.get("service") for s in services}

    try:
        doc = await custom_providers_collection().find_one({"provider_key": provider})
        if doc:
            doc = _serialize(doc)
            sdk_pkg = " ".join(d["name"] for d in doc.get("dependencies", []))
            for svc in doc.get("services", []):
                key = svc.get("service_key", "")
                if not key or key in seen_keys:
                    continue
                services.append({
                    "provider": provider,
                    "service": key,
                    "service_key": key,
                    "display_name": svc.get("display_name", ""),
                    "description": svc.get("description", ""),
                    "auth_type": svc.get("auth_type", "api_key"),
                    "category": svc.get("category", "general"),
                    "logo_url": svc.get("logo_url", ""),
                    "sdk_package": sdk_pkg,
                    "is_custom": True,
                })
                seen_keys.add(key)
    except Exception as exc:
        logger.warning("catalog.custom_service_fetch_failed", provider=provider, error=str(exc))

    if not services:
        logger.warning("catalog.provider_not_found", provider=provider)
        raise HTTPException(404, f"Provider '{provider}' not found in catalog")

    services.sort(key=lambda s: (s.get("display_name") or s.get("service") or "").lower())
    logger.info("catalog.list_services", provider=provider, count=len(services))
    return services


@catalog_router.get("/services/{provider}/{service}")
async def service_detail(provider: str, service: str):
    """Return detailed metadata for one service."""
    detail = get_service_detail(provider, service)
    if detail:
        logger.info("catalog.service_detail", provider=provider, service=service)
        return detail
    logger.warning("catalog.service_not_found", provider=provider, service=service)
    raise HTTPException(404, f"Service '{provider}/{service}' not found in catalog")


# ── Single provider GET/PATCH (works for both static and custom) ───────

@catalog_router.get("/providers/{provider_key}/detail")
async def get_provider_detail(provider_key: str):
    """Get full detail for a single provider (static, custom, or static-with-override)."""
    # Check static catalog
    static_services = get_provider_services(provider_key)
    if static_services:
        # Static provider — fetch override if any
        override = None
        try:
            ov = await static_provider_overrides_collection().find_one({"provider_key": provider_key})
            if ov:
                override = _serialize(ov)
        except Exception:
            pass

        base_display = provider_key.replace("_", " ").title()
        return {
            "provider": provider_key,
            "display_name": (override or {}).get("display_name") or base_display,
            "description": (override or {}).get("description") or "",
            "logo_url": (override or {}).get("logo_url") or "",
            "brand_color": (override or {}).get("brand_color") or "#14B8A6",
            "website_url": (override or {}).get("website_url") or "",
            "is_custom": False,
            "override_id": (override or {}).get("id") or None,
            "modified_by": (override or {}).get("modified_by") or "",
            "modified_at": (override or {}).get("modified_at") or "",
            "services": static_services,
            "extra_services": (override or {}).get("extra_services") or [],
            "dependencies": (override or {}).get("dependencies") or [],
        }

    # Check custom providers
    try:
        doc = await custom_providers_collection().find_one({"provider_key": provider_key})
        if doc:
            doc = _serialize(doc)
            return {
                "provider": provider_key,
                "display_name": doc.get("display_name", ""),
                "description": doc.get("description", ""),
                "logo_url": doc.get("logo_url", ""),
                "brand_color": doc.get("brand_color", "#14B8A6"),
                "website_url": doc.get("website_url", ""),
                "is_custom": True,
                "custom_id": doc["id"],
                "modified_by": doc.get("modified_by", ""),
                "modified_at": doc.get("modified_at", ""),
                "services": doc.get("services", []),
                "extra_services": [],
                "dependencies": doc.get("dependencies", []),
            }
    except Exception as exc:
        logger.warning("catalog.provider_detail_custom_failed", provider=provider_key, error=str(exc))

    raise HTTPException(404, f"Provider '{provider_key}' not found")


class UpdateAnyProviderRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    website_url: Optional[str] = None
    brand_color: Optional[str] = None
    logo_url: Optional[str] = None
    extra_services: Optional[List[ServiceInput]] = None  # additional services for static providers
    services: Optional[List[ServiceInput]] = None        # full services list for custom providers
    dependencies: Optional[List[DependencyInput]] = None


@catalog_router.patch("/providers/{provider_key}")
async def update_any_provider(
    provider_key: str,
    body: UpdateAnyProviderRequest,
    x_user_email: Optional[str] = Header(None),
):
    """Update any provider — static (creates/updates override) or custom."""
    now = datetime.now(timezone.utc).isoformat()
    modified_by = x_user_email or "super-admin"

    # Check if static
    static_services = get_provider_services(provider_key)
    if static_services:
        updates: Dict[str, Any] = {
            "provider_key": provider_key,
            "modified_by": modified_by,
            "modified_at": now,
        }
        if body.display_name is not None:
            updates["display_name"] = body.display_name
        if body.description is not None:
            updates["description"] = body.description
        if body.website_url is not None:
            updates["website_url"] = body.website_url
        if body.brand_color is not None:
            updates["brand_color"] = body.brand_color
        if body.logo_url is not None:
            updates["logo_url"] = body.logo_url
        if body.extra_services is not None:
            updates["extra_services"] = [s.model_dump() for s in body.extra_services]
        if body.dependencies is not None:
            updates["dependencies"] = [d.model_dump() for d in body.dependencies]

        result = await static_provider_overrides_collection().update_one(
            {"provider_key": provider_key},
            {"$set": updates},
            upsert=True,
        )
        ov = await static_provider_overrides_collection().find_one({"provider_key": provider_key})
        logger.info("catalog.static_provider_updated", provider=provider_key, modified_by=modified_by)
        return _serialize(ov)

    # Custom provider update
    try:
        doc = await custom_providers_collection().find_one({"provider_key": provider_key})
        if not doc:
            raise HTTPException(404, f"Provider '{provider_key}' not found")
        updates = {"modified_by": modified_by, "modified_at": now}
        if body.display_name is not None:
            updates["display_name"] = body.display_name
        if body.description is not None:
            updates["description"] = body.description
        if body.website_url is not None:
            updates["website_url"] = body.website_url
        if body.brand_color is not None:
            updates["brand_color"] = body.brand_color
        if body.logo_url is not None:
            updates["logo_url"] = body.logo_url
        if body.services is not None:
            updates["services"] = [s.model_dump() for s in body.services]
        if body.dependencies is not None:
            updates["dependencies"] = [d.model_dump() for d in body.dependencies]

        await custom_providers_collection().update_one(
            {"provider_key": provider_key}, {"$set": updates}
        )
        updated = await custom_providers_collection().find_one({"provider_key": provider_key})
        logger.info("catalog.custom_provider_updated_by_key", provider=provider_key)
        return _serialize(updated)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("catalog.update_any_provider_failed", error=str(exc))
        raise HTTPException(500, f"Update failed: {exc}")


# ── Custom provider CRUD ──────────────────────────────────────────────

@catalog_router.post("/custom-providers")
async def create_custom_provider(body: CreateCustomProviderRequest):
    """Create a new user-defined provider with services and dependencies."""
    provider_key = _provider_key(body.display_name)

    # If the key conflicts with the static catalog, suffix with _custom
    if provider_key in SERVICE_CATALOG:
        provider_key = f"{provider_key}_custom"

    # Ensure unique key within custom providers
    base_key = provider_key
    suffix = 2
    while await custom_providers_collection().find_one({"provider_key": provider_key}):
        provider_key = f"{base_key}_{suffix}"
        suffix += 1

    doc = {
        "provider_key": provider_key,
        "display_name": body.display_name,
        "description": body.description,
        "website_url": body.website_url or "",
        "brand_color": body.brand_color or "#14B8A6",
        "logo_url": body.logo_url or "",
        "services": [s.model_dump() for s in body.services],
        "dependencies": [d.model_dump() for d in body.dependencies],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    result = await custom_providers_collection().insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    doc["id"] = doc["_id"]

    logger.info(
        "catalog.custom_provider_created",
        provider_key=provider_key,
        id=doc["id"],
        services=len(body.services),
    )
    return doc


@catalog_router.get("/custom-providers")
async def list_custom_providers():
    """List all user-defined providers."""
    docs = await custom_providers_collection().find({}).to_list(None)
    return [_serialize(d) for d in docs]


@catalog_router.get("/custom-providers/{provider_id}")
async def get_custom_provider(provider_id: str):
    """Get a single custom provider by ID."""
    try:
        doc = await custom_providers_collection().find_one({"_id": ObjectId(provider_id)})
    except Exception:
        raise HTTPException(400, "Invalid provider ID")
    if not doc:
        raise HTTPException(404, "Custom provider not found")
    return _serialize(doc)


@catalog_router.patch("/custom-providers/{provider_id}")
async def update_custom_provider(provider_id: str, body: UpdateCustomProviderRequest):
    """Update a custom provider's details, services, or dependencies."""
    try:
        oid = ObjectId(provider_id)
    except Exception:
        raise HTTPException(400, "Invalid provider ID")

    updates: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}

    if body.display_name is not None:
        updates["display_name"] = body.display_name
        updates["provider_key"] = _provider_key(body.display_name)
    if body.description is not None:
        updates["description"] = body.description
    if body.website_url is not None:
        updates["website_url"] = body.website_url
    if body.brand_color is not None:
        updates["brand_color"] = body.brand_color
    if body.logo_url is not None:
        updates["logo_url"] = body.logo_url
    if body.services is not None:
        updates["services"] = [s.model_dump() for s in body.services]
    if body.dependencies is not None:
        updates["dependencies"] = [d.model_dump() for d in body.dependencies]

    result = await custom_providers_collection().update_one({"_id": oid}, {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(404, "Custom provider not found")

    doc = await custom_providers_collection().find_one({"_id": oid})
    logger.info("catalog.custom_provider_updated", provider_id=provider_id)
    return _serialize(doc)


@catalog_router.delete("/custom-providers/{provider_id}")
async def delete_custom_provider(provider_id: str):
    """Delete a custom provider."""
    try:
        oid = ObjectId(provider_id)
    except Exception:
        raise HTTPException(400, "Invalid provider ID")

    result = await custom_providers_collection().delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Custom provider not found")

    logger.info("catalog.custom_provider_deleted", provider_id=provider_id)
    return {"deleted": True, "id": provider_id}


# ── AI suggestion endpoints ───────────────────────────────────────────

_SUGGEST_SERVICES_SYSTEM = """You are an expert software integration architect.
Given a provider/platform name and description, suggest realistic API services that this provider would offer.
Return ONLY a valid JSON array — no markdown, no explanation.
Each service object must have:
  service_key (snake_case slug, unique within the list),
  display_name (short human label),
  description (1-2 sentence what the service does),
  auth_type (one of: oauth2, api_key, bearer_token, basic, service_account),
  category (one of: productivity, storage, communication, payments, crm, data, cloud, analytics, identity, social, iot, maps, general),
  suggested_sdk (the main Python PyPI package name, if known, otherwise empty string).
Suggest 6–12 services that make practical sense for this provider."""

_SUGGEST_DEPS_SYSTEM = """You are a Python dependency expert for API integrations.
Given a provider name and a list of its services (with auth types), suggest the Python PyPI packages needed to build connectors.
Return ONLY a valid JSON object — no markdown, no explanation.
Format:
{
  "packages": [
    {"name": "package-name", "version": ">=x.y", "reason": "why it is needed"}
  ]
}
Include: HTTP clients (httpx or requests), auth helpers (authlib, google-auth, etc.), official SDKs if available.
Limit to 4–8 packages. Prefer widely-used, well-maintained packages."""


@catalog_router.post("/ai/suggest-services")
async def suggest_services(body: SuggestServicesRequest):
    """Ask AI to suggest services for a new custom provider."""
    prompt = (
        f"Provider: {body.provider_name}\n"
        f"Description: {body.description}\n"
    )
    if body.website_url:
        prompt += f"Website: {body.website_url}\n"
    prompt += "\nSuggest services for this provider. Return a JSON array."

    try:
        _system = await r2_service.get_step_prompt("SUGGEST_SERVICES_SYSTEM", _SUGGEST_SERVICES_SYSTEM)
        result = await call_llm_json(
            messages=[{"role": "user", "content": prompt}],
            system=_system,
            max_tokens=4096,
            temperature=0.3,
        )
        # Result can be a list directly or {"services": [...]}
        services = result if isinstance(result, list) else result.get("services", result)
        logger.info("catalog.ai_suggest_services", provider=body.provider_name, count=len(services))
        return {"services": services}
    except Exception as exc:
        logger.error("catalog.ai_suggest_services_failed", error=str(exc))
        raise HTTPException(500, f"AI service suggestion failed: {exc}")


@catalog_router.post("/ai/suggest-dependencies")
async def suggest_dependencies(body: SuggestDependenciesRequest):
    """Ask AI to suggest Python package dependencies for the selected services."""
    service_summary = "\n".join(
        f"- {s.get('display_name', s.get('service_key', ''))} (auth: {s.get('auth_type', 'api_key')})"
        for s in body.services
    )
    prompt = (
        f"Provider: {body.provider_name}\n"
        f"Selected services:\n{service_summary}\n\n"
        "Suggest Python PyPI packages needed to build connectors for these services."
    )
    if body.custom_prompt and body.custom_prompt.strip():
        prompt += f"\n\nAdditional requirement from the user: {body.custom_prompt.strip()}"

    if body.attached_docs:
        for i, doc in enumerate(body.attached_docs, 1):
            # Truncate each doc to 4000 chars to stay within token budget
            snippet = doc.strip()[:4000]
            prompt += f"\n\n--- Attached reference document {i} ---\n{snippet}\n--- end of document {i} ---"

    try:
        _system = await r2_service.get_step_prompt("SUGGEST_DEPS_SYSTEM", _SUGGEST_DEPS_SYSTEM)
        result = await call_llm_json(
            messages=[{"role": "user", "content": prompt}],
            system=_system,
            max_tokens=2048,
            temperature=0.2,
        )
        packages = result.get("packages", result) if isinstance(result, dict) else result
        logger.info("catalog.ai_suggest_deps", provider=body.provider_name, count=len(packages))
        return {"packages": packages}
    except Exception as exc:
        logger.error("catalog.ai_suggest_deps_failed", error=str(exc))
        raise HTTPException(500, f"AI dependency suggestion failed: {exc}")


@catalog_router.post("/ai/validate-package")
async def validate_package(body: ValidatePackageRequest):
    """Ask AI to validate whether a user-supplied PyPI package name is real and relevant."""
    prompt = (
        f"The user wants to use Python package '{body.package_name}' for the '{body.provider_name}' integration.\n"
        "Is this a real, published PyPI package? Is it relevant for API integration?\n"
        'Return JSON: {"valid": true/false, "reason": "short explanation", "canonical_name": "correct pypi name if different"}'
    )
    try:
        result = await call_llm_json(
            messages=[{"role": "user", "content": prompt}],
            system="You are a Python package expert. Answer concisely in JSON only.",
            max_tokens=256,
            temperature=0.1,
        )
        return result
    except Exception as exc:
        logger.error("catalog.ai_validate_package_failed", error=str(exc))
        # Fail open — let the user use it
        return {"valid": True, "reason": "Could not validate — proceeding.", "canonical_name": body.package_name}
