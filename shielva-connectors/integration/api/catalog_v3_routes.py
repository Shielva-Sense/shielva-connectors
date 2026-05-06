"""Catalog v3 — global (non-tenant) catalog API for the Shielva Agentic Developer desktop app.

Logo strategy:
  - One-time seed: POST /api/v3/catalog/logos/seed  →  generates SVG logos for all providers,
    uploads to R2 at  catalog/logos/{key}.svg,  returns count.
  - v3_list_providers injects  logo_url  pointing to the gateway CDN path so the
    Electron app stores the URL in its 1-hour disk cache alongside provider metadata.
  - ProviderLogo component tries logo_url (img tag) first, falls back to SVG paths
    in provider-logos.ts, then brand-colored initials.


Provider list is the union of:
  1. Static Python catalog  (integration/data/catalog.py)
  2. connector_catalog.json (413 providers with brand colors)
  3. Custom providers in MongoDB

Brand colors and descriptions come from connector_catalog.json when available.
"""

import asyncio
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
import structlog
from integration.data.catalog import get_all_providers, get_provider_services
from integration.db.database import custom_providers_collection
from integration.services import r2_service

logger = structlog.get_logger(__name__)

catalog_v3_router = APIRouter(prefix="/api/v3/catalog", tags=["catalog-v3"])

# ── Load connector_catalog.json once at module level ───────────────────────
_CATALOG_JSON_PATH = Path(__file__).parent.parent / "data" / "connector_catalog.json"

def _load_connector_catalog() -> dict:
    """Load connector_catalog.json keyed by provider key."""
    try:
        with open(_CATALOG_JSON_PATH) as f:
            raw = json.load(f)
        providers = raw if isinstance(raw, list) else raw.get("providers", [])
        return {p["key"]: p for p in providers if "key" in p}
    except Exception as exc:
        logger.warning("catalog_v3.connector_catalog_load_failed", error=str(exc))
        return {}

_CONNECTOR_CATALOG: dict = _load_connector_catalog()

# R2 logo path — stored at shielva-sense / shielva-platform-int/Connector/logos/{key}.svg
_LOGO_R2_PREFIX = "shielva-platform-int/Connector/logos"
_LOGO_R2_BUCKET = "shielvasense"
# Gateway CDN URL prefix served by the api-gateway
_LOGO_CDN_PREFIX = "/integration/api/v3/catalog/logos"


def _logo_cdn_url(provider_key: str) -> str:
    """Return the CDN URL for a provider logo (served by gateway from R2)."""
    return f"{_LOGO_CDN_PREFIX}/{provider_key}.svg"


def _r2_logo_key(provider_key: str) -> str:
    return f"{_LOGO_R2_PREFIX}/{provider_key}.svg"


def _generate_svg_logo(display_name: str, brand_color: str) -> str:
    """Generate a simple SVG logo: brand-colored rounded rect + white initial letters."""
    color = brand_color.lstrip("#")
    # Pick 1-2 letter abbreviation
    words = display_name.split()
    if len(words) >= 2:
        label = (words[0][0] + words[1][0]).upper()
    else:
        label = display_name[:2].upper()
    font_size = 20 if len(label) == 1 else 16
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">'
        f'<rect width="48" height="48" rx="10" fill="#{color}"/>'
        f'<text x="24" y="{24 + font_size // 3}" text-anchor="middle" dominant-baseline="middle" '
        f'font-size="{font_size}" font-weight="800" font-family="system-ui,sans-serif" fill="white">{label}</text>'
        f'</svg>'
    )


def _provider_key(display_name: str) -> str:
    return display_name.lower().replace(" ", "_").replace("-", "_")


def _merge_brand(provider_dict: dict) -> dict:
    """Merge brand_color and description from connector_catalog.json if available."""
    key = provider_dict.get("key") or provider_dict.get("provider", "")
    meta = _CONNECTOR_CATALOG.get(key, {})
    if meta.get("brand_color") and not provider_dict.get("brand_color"):
        provider_dict["brand_color"] = meta["brand_color"]
    if meta.get("description") and not provider_dict.get("description"):
        provider_dict["description"] = meta["description"]
    return provider_dict


def _build_services_for_static_provider(key: str) -> list:
    """Return normalized services list for a static catalog provider."""
    services = get_provider_services(key)
    if not services:
        return []
    for s in services:
        if "key" not in s:
            s["key"] = s.get("service", s.get("service_key", ""))
    return services


@catalog_v3_router.get("/providers")
async def v3_list_providers():
    """Return all providers with embedded services (static + connector_catalog.json + custom DB).

    Embeds services[] on each provider so the frontend makes exactly ONE call
    to get providers + all services — no per-provider service fetches needed.
    """
    seen_keys: set = set()
    result = []

    # 1. Static Python catalog providers — embed services inline
    for p in get_all_providers():
        key = p.get("provider", "")
        p["key"] = key
        p = _merge_brand(p)
        p["logo_url"] = _logo_cdn_url(key)
        p["services"] = _build_services_for_static_provider(key)
        seen_keys.add(key)
        result.append(p)

    # 2. connector_catalog.json — providers NOT already in static catalog
    for key, meta in _CONNECTOR_CATALOG.items():
        if key in seen_keys:
            continue
        result.append({
            "key": key,
            "provider": key,
            "display_name": meta.get("display_name", key.replace("_", " ").title()),
            "description": meta.get("description", ""),
            "brand_color": meta.get("brand_color", "#14B8A6"),
            "service_count": 0,
            "logo_url": _logo_cdn_url(key),
            "is_custom": False,
            "services": [],
        })
        seen_keys.add(key)

    # 3. Custom providers from MongoDB — embed their services inline
    try:
        customs = await custom_providers_collection().find({}).to_list(None)
        for c in customs:
            key = c.get("provider_key", _provider_key(c.get("display_name", "")))
            raw_services = c.get("services", [])
            services = [
                {
                    "key": svc.get("service_key", ""),
                    "service": svc.get("service_key", ""),
                    "display_name": svc.get("display_name", ""),
                    "description": svc.get("description", ""),
                    "auth_type": svc.get("auth_type", "api_key"),
                    "category": svc.get("category", "general"),
                    "logo_url": svc.get("logo_url", ""),
                    "is_custom": True,
                }
                for svc in raw_services
            ]
            entry = {
                "key": key,
                "provider": key,
                "display_name": c.get("display_name", ""),
                "service_count": len(services),
                "logo_url": c.get("logo_url", ""),
                "is_custom": True,
                "custom_id": str(c["_id"]) if "_id" in c else "",
                "brand_color": c.get("brand_color", "#14B8A6"),
                "description": c.get("description", ""),
                "services": services,
            }
            entry = _merge_brand(entry)
            if key not in seen_keys:
                result.append(entry)
                seen_keys.add(key)
            else:
                for i, r in enumerate(result):
                    if r.get("key") == key:
                        result[i] = entry
                        break
    except Exception as exc:
        logger.warning("catalog_v3.custom_providers_fetch_failed", error=str(exc))

    result = sorted(result, key=lambda p: p["display_name"].lower())
    logger.info("catalog_v3.list_providers", count=len(result))
    return {"providers": result, "count": len(result)}


@catalog_v3_router.get("/providers/{provider}/services")
async def v3_list_services(provider: str):
    """Return services for a provider. No tenant scope."""
    # 1. Static Python catalog
    services = get_provider_services(provider)
    if services:
        # Normalize: ensure each service has a "key" field
        for s in services:
            if "key" not in s:
                s["key"] = s.get("service", s.get("service_key", ""))
        logger.info("catalog_v3.list_services", provider=provider, count=len(services), source="static")
        return {"services": services, "provider": provider}

    # 2. Custom MongoDB provider
    try:
        doc = await custom_providers_collection().find_one({"provider_key": provider})
        if doc:
            result = []
            for svc in doc.get("services", []):
                result.append({
                    "key": svc.get("service_key", ""),
                    "service": svc.get("service_key", ""),
                    "display_name": svc.get("display_name", ""),
                    "description": svc.get("description", ""),
                    "auth_type": svc.get("auth_type", "api_key"),
                    "category": svc.get("category", "general"),
                    "logo_url": svc.get("logo_url", ""),
                    "is_custom": True,
                })
            logger.info("catalog_v3.list_services", provider=provider, count=len(result), source="custom")
            return {"services": result, "provider": provider}
    except Exception as exc:
        logger.warning("catalog_v3.custom_service_fetch_failed", provider=provider, error=str(exc))

    # 3. Provider exists in connector_catalog.json but has no services yet
    if provider in _CONNECTOR_CATALOG:
        logger.info("catalog_v3.list_services", provider=provider, count=0, source="catalog_json")
        return {"services": [], "provider": provider}

    raise HTTPException(404, f"Provider '{provider}' not found")


# ── Logo endpoints ────────────────────────────────────────────────────────────

@catalog_v3_router.get("/logos/{key}.svg")
async def v3_get_logo(key: str):
    """Serve a provider logo SVG from R2. Falls back to generated SVG if not in R2."""
    if not r2_service._use_local():
        try:
            client = r2_service._get_client()
            r2_key = _r2_logo_key(key)
            svg = r2_service._sync_read(client, _LOGO_R2_BUCKET, r2_key)
            if svg:
                return Response(content=svg, media_type="image/svg+xml",
                                headers={"Cache-Control": "public, max-age=86400"})
        except Exception as exc:
            logger.warning("catalog_v3.logo_r2_read_failed", key=key, error=str(exc))

    # Fallback: generate on the fly from catalog metadata
    meta = _CONNECTOR_CATALOG.get(key, {})
    display_name = meta.get("display_name", key.replace("_", " ").title())
    brand_color = meta.get("brand_color", "#14B8A6")
    svg = _generate_svg_logo(display_name, brand_color)
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@catalog_v3_router.post("/logos/seed")
async def v3_seed_logos():
    """One-time: generate SVG logos for all providers and upload to R2.

    Uploads to R2 at:  shielva-sense / shielva-platform-int/Connector/logos/{key}.svg

    Safe to call multiple times — existing logos are overwritten.
    """
    if r2_service._use_local():
        raise HTTPException(503, "R2 is not configured — cannot seed logos")

    # Collect all provider keys + metadata
    all_providers: dict[str, dict] = {}
    for p in get_all_providers():
        key = p.get("provider", "")
        if key:
            all_providers[key] = p
    for key, meta in _CONNECTOR_CATALOG.items():
        if key not in all_providers:
            all_providers[key] = meta

    client = r2_service._get_client()
    uploaded = 0
    failed = 0

    async def _upload_one(key: str, meta: dict):
        nonlocal uploaded, failed
        display_name = meta.get("display_name", key.replace("_", " ").title())
        brand_color = meta.get("brand_color", "#14B8A6")
        svg = _generate_svg_logo(display_name, brand_color)
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: r2_service._sync_write(client, _LOGO_R2_BUCKET, _r2_logo_key(key), svg, "image/svg+xml")
            )
            uploaded += 1
        except Exception as exc:
            logger.warning("catalog_v3.logo_seed_failed", key=key, error=str(exc))
            failed += 1

    # Upload concurrently in batches of 20
    keys = list(all_providers.keys())
    batch_size = 20
    for i in range(0, len(keys), batch_size):
        batch = keys[i:i + batch_size]
        await asyncio.gather(*[_upload_one(k, all_providers[k]) for k in batch])

    logger.info("catalog_v3.logos_seeded", uploaded=uploaded, failed=failed, total=len(keys))
    return {"uploaded": uploaded, "failed": failed, "total": len(keys)}
