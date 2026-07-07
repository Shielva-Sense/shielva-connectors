#!/usr/bin/env python3
"""Seed the Integration-Builder catalog + sessions from a SOURCE Mongo into a TARGET.

Why this exists: the Integration Builder pages (categories / providers / services) and
the ACP Connectors "Advanced" tab read Mongo `ShielvaIntegration`, not the gateway
registry. A fresh prod DB is empty, so those pages render nothing. This copies the
data the local build produced into the target DB.

Two classes of data — handled differently:

  GLOBAL (tenant-agnostic, copied verbatim, idempotent upsert on _id):
    provider_categories            — the category list (Integration Builder "categories")
    provider_category_map          — provider→category mapping ("providers")
    static_provider_overrides
    connector_documentation_guidelines, connector_guidelines, metadata_writing_guidelines
    sync_settings

  TENANT-SCOPED (the built connectors — re-keyed to the target tenant/app):
    integration_sessions           — 1 per built connector ("services").
      tenant_id / tenant_name / app_id are REMAPPED to the target so the prod tenant
      can see them. NOTE: each session also carries gateway_connector_id + R2
      heavy-field keys; those are NOT rewritten here (R2 objects are global, and the
      gateway registry is fed separately by the JFrog artifacts).

Usage (dry-run first — counts only, writes nothing):

    export SRC_MONGO='mongodb+srv://…/'          # source (default: the local .env URL)
    export DST_MONGO='mongodb+srv://…prod…/'      # TARGET — required to write
    export SRC_DB=ShielvaIntegration DST_DB=ShielvaIntegration
    export OLD_TENANT=Tenant-f9184cb7
    export NEW_TENANT=Tenant-90de08d4             # the prod tenant
    export NEW_APP_ID=<prod app_id>               # optional; omit to keep source app_id
    python core/seed_integration_to_prod.py            # dry-run
    python core/seed_integration_to_prod.py --apply    # actually write

    # only the global catalog (skip the tenant-scoped sessions):
    python core/seed_integration_to_prod.py --apply --catalog-only
"""

from __future__ import annotations

import argparse
import os

GLOBAL_COLLECTIONS = [
    "provider_categories",
    "provider_category_map",
    "static_provider_overrides",
    "connector_documentation_guidelines",
    "connector_guidelines",
    "metadata_writing_guidelines",
    "sync_settings",
]
SESSIONS = "integration_sessions"

_LOCAL_DEFAULT = "mongodb+srv://shielvaadmin:shielvaadmin123@mastershielva.8rbs44q.mongodb.net/?appName=MasterShielva"


def _upsert_all(dst_col, docs) -> int:
    from pymongo import ReplaceOne

    ops = [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs if "_id" in d]
    if not ops:
        return 0
    res = dst_col.bulk_write(ops, ordered=False)
    return (res.upserted_count or 0) + (res.modified_count or 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually write (default is dry-run)")
    ap.add_argument(
        "--catalog-only",
        action="store_true",
        help="skip the tenant-scoped integration_sessions",
    )
    args = ap.parse_args()

    from pymongo import MongoClient

    src_url = os.environ.get("SRC_MONGO", _LOCAL_DEFAULT)
    dst_url = os.environ.get("DST_MONGO", "")
    src_db_name = os.environ.get("SRC_DB", "ShielvaIntegration")
    dst_db_name = os.environ.get("DST_DB", "ShielvaIntegration")
    old_tenant = os.environ.get("OLD_TENANT", "Tenant-f9184cb7")
    new_tenant = os.environ.get("NEW_TENANT", "")
    new_app_id = os.environ.get("NEW_APP_ID", "")

    if args.apply and not dst_url:
        print("✗ --apply needs DST_MONGO (the target/prod Mongo URL)")
        return 2
    if not args.catalog_only and args.apply and not new_tenant:
        print("✗ seeding sessions needs NEW_TENANT (the prod tenant) — or pass --catalog-only")
        return 2

    src = MongoClient(src_url, serverSelectionTimeoutMS=8000)[src_db_name]
    dst = MongoClient(dst_url, serverSelectionTimeoutMS=8000)[dst_db_name] if dst_url else None

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"== {mode} ==  src={src_db_name}  dst={dst_db_name or '(none)'}  "
        f"tenant {old_tenant}→{new_tenant or '(unchanged)'}  app_id→{new_app_id or '(unchanged)'}"
    )

    print("\n-- global catalog --")
    for name in GLOBAL_COLLECTIONS:
        docs = list(src[name].find({}))
        if not args.apply:
            print(f"  {name}: {len(docs)} (would upsert)")
            continue
        n = _upsert_all(dst[name], docs)
        print(f"  {name}: upserted {n}/{len(docs)}")

    if not args.catalog_only:
        print("\n-- integration_sessions (tenant-remapped) --")
        src_docs = list(src[SESSIONS].find({"tenant_id": old_tenant}))
        if not args.apply:
            print(f"  {SESSIONS}: {len(src_docs)} sessions under {old_tenant} (would remap → {new_tenant})")
        else:
            for d in src_docs:
                d["tenant_id"] = new_tenant
                if new_app_id:
                    d["app_id"] = new_app_id
                d["tenant_name"] = new_tenant
            n = _upsert_all(dst[SESSIONS], src_docs)
            print(f"  {SESSIONS}: upserted {n}/{len(src_docs)} under {new_tenant}")

    print("\n✓ done" + ("" if args.apply else " (dry-run — nothing written; add --apply)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
