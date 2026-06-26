"""Advanced-connector catalog: deploy-time seed from a baked snapshot to Mongo.

Pipeline:

    metadata/connector.json   (one per connector source dir — source of truth)
        │
        ├─ build_artifact.py   (consolidates → core/shielva_connectors.json)
        │
        ▼
    shielva_connectors.json   (baked into the image; one file = fast verify)
        │
        ▼  gateway lifespan
    seed_catalog_if_needed()
        ├─ hash matches Mongo marker → skip (fast path; the per-file walk over
        │   ~200 metadata/connector.json never happens at runtime)
        └─ hash differs              → bulk-upsert each connector + new marker

The snapshot is the canonical fast verifier at boot. The Mongo collection is what
the ACP UI reads (via `/connectors/types`) to render the rich advanced-connector
catalog (display_name, description, auth_type, oauth_scopes, apis, install_fields,
…) without per-pod filesystem reads.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Snapshot lives next to gateway.py (the build_artifact.py default).
_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "shielva_connectors.json"

_DB_NAME = os.getenv("CATALOG_DB", os.getenv("MONGODB_DB", "ShielvaIntegration"))
_COLL_NAME = os.getenv("CATALOG_COLLECTION", "advanced_connector_catalog")
_META_ID = "__catalog_meta__"


def _snapshot_hash(payload: dict) -> str:
    """Stable content hash over the connector list (order-independent)."""
    items = payload.get("connectors", []) or []
    body = json.dumps(sorted(items, key=lambda c: c.get("connector_type") or ""),
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def load_snapshot() -> dict | None:
    """Read the baked snapshot. None when the image was built without one
    (e.g. local dev) — seeding is a no-op in that case."""
    if not _SNAPSHOT_PATH.exists():
        logger.info("connector_catalog.snapshot_missing", path=str(_SNAPSHOT_PATH))
        return None
    try:
        return json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("connector_catalog.snapshot_unreadable", error=str(exc)[:200])
        return None


async def seed_catalog_if_needed() -> dict:
    """Run at gateway startup. Idempotent + hash-gated."""
    payload = load_snapshot()
    if not payload:
        return {"seeded": 0, "skipped": "snapshot_missing"}

    url = os.environ.get("MONGODB_URL", "").strip()
    if not url:
        logger.info("connector_catalog.seed_skipped", reason="MONGODB_URL unset")
        return {"seeded": 0, "skipped": "no_mongo"}

    new_hash = _snapshot_hash(payload)
    connectors = payload.get("connectors", []) or []

    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        from pymongo import ReplaceOne
    except ImportError:
        logger.error("connector_catalog.seed_skipped", reason="motor/pymongo unavailable")
        return {"seeded": 0, "skipped": "no_driver"}

    client = AsyncIOMotorClient(url, serverSelectionTimeoutMS=8000)
    try:
        coll = client[_DB_NAME][_COLL_NAME]

        # Fast path — marker hash matches the snapshot → catalog already current.
        marker = await coll.find_one({"_id": _META_ID})
        if marker and marker.get("hash") == new_hash:
            logger.info("connector_catalog.up_to_date",
                        count=len(connectors), hash=new_hash[:8])
            return {"seeded": 0, "skipped": "hash_match", "count": len(connectors)}

        # Bulk-upsert: _id = connector_type, payload = the raw metadata doc.
        ops = []
        for c in connectors:
            ctype = c.get("connector_type") or c.get("type")
            if not ctype:
                continue
            doc = dict(c)
            doc["_id"] = ctype
            ops.append(ReplaceOne({"_id": ctype}, doc, upsert=True))
        if ops:
            await coll.bulk_write(ops, ordered=False)

        await coll.replace_one(
            {"_id": _META_ID},
            {"_id": _META_ID, "hash": new_hash, "count": len(ops),
             "snapshot_version": payload.get("version"),
             "seeded_at": datetime.now(timezone.utc).isoformat()},
            upsert=True,
        )
        logger.info("connector_catalog.seeded", count=len(ops), hash=new_hash[:8],
                    db=_DB_NAME, collection=_COLL_NAME)
        return {"seeded": len(ops), "hash": new_hash, "count": len(connectors)}
    except Exception as exc:  # noqa: BLE001 — seed must never crash boot
        logger.error("connector_catalog.seed_failed", error=str(exc)[:300])
        return {"seeded": 0, "skipped": "error", "error": str(exc)[:200]}
    finally:
        client.close()


async def list_catalog() -> list[dict]:
    """Return every seeded connector doc (sans `_id` + meta). Empty if no Mongo."""
    url = os.environ.get("MONGODB_URL", "").strip()
    if not url:
        return []
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        return []
    client = AsyncIOMotorClient(url, serverSelectionTimeoutMS=4000)
    try:
        coll = client[_DB_NAME][_COLL_NAME]
        out: list[dict] = []
        async for d in coll.find({"_id": {"$ne": _META_ID}}):
            d.pop("_id", None)
            out.append(d)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("connector_catalog.list_failed", error=str(exc)[:200])
        return []
    finally:
        client.close()
