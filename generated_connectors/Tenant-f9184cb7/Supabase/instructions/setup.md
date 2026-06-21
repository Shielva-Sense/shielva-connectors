# Setup Instructions: Supabase

## Overview

The Supabase connector integrates a Shielva tenant with a single Supabase project
through four surfaces:

- **PostgREST** (`/rest/v1/{table}`) — full CRUD on any table in the configured schema.
- **Auth Admin** (`/auth/v1/admin/users`) — list, get, create, update, and delete users.
- **Storage** (`/storage/v1/{bucket,object}`) — buckets, objects, signed URLs.
- **Edge Functions** (`/functions/v1/{name}`) — invoke Deno-runtime serverless functions.

Authentication is the project's **service-role API key**, sent as BOTH the
`apikey` header and the `Authorization: Bearer <key>` header on every request.
The service-role key bypasses Row-Level Security — treat it as a master secret.

---

## Prerequisites

- A Supabase project (https://supabase.com).
- Owner access to **Project Settings → API**.

---

## Step 1: Project URL (`project_url`) — **Required**

1. Open your Supabase project dashboard.
2. Go to **Project Settings → API**.
3. Copy the **Project URL** — it looks like `https://abcdwxyz.supabase.co`.
4. Paste it into the **Project URL** field in Shielva.

> Legacy compatibility: the connector also accepts a `project_ref` short form
> (e.g. `abcdwxyz`) and constructs the URL itself.

---

## Step 2: Service Role Key (`service_role_key`) — **Required**

1. On the same **Project Settings → API** page, scroll to **Project API keys**.
2. Copy the **`service_role` secret** (NOT the `anon` key). The key starts with
   `eyJ...` and is the long-lived JWT.
3. Paste it into the **Service Role Key** field in Shielva (type `secret`).

> **Important:**
> - The service-role key bypasses Row-Level Security. NEVER paste it into a
>   browser, mobile app, or any client-side bundle.
> - Shielva stores the key via `BaseConnector.save_config()` which routes through
>   the platform's sealed-config / vault sidecar (port 8054).
> - If you suspect a leak, rotate the key from the same Settings page.

---

## Step 3: Schema (`schema`) — Optional

Defaults to `public`. PostgREST sends the schema as `Accept-Profile` /
`Content-Profile` headers; set this only if your tables live in a non-default
schema (e.g. `app`, `tenant_xyz`).

---

## Step 4: Rate Limit (`rate_limit_per_min`) — Optional

Defaults to `100`. This is a client-side soft cap; Supabase's platform-level
rate limits still apply.

---

## Verification

After saving the config, the connector runs `health_check()` automatically.
It hits `GET /auth/v1/settings` — a lightweight, RLS-free probe.

| Outcome | Meaning |
|---|---|
| `HEALTHY + CONNECTED` | API key accepted; ready for use. |
| `DEGRADED + INVALID_CREDENTIALS` | 401 — key rotated or expired. Regenerate and re-save. |
| `UNHEALTHY + INVALID_CREDENTIALS` | 403 — you may have copied the `anon` key instead of `service_role`. |
| `OFFLINE + FAILED` | Network / Supabase outage. Retry in a few minutes. |

---

## Quick reference

```python
from connector import SupabaseConnector

conn = SupabaseConnector(
    tenant_id="t-1",
    connector_id="c-1",
    config={
        "project_url": "https://abcdwxyz.supabase.co",
        "service_role_key": "eyJ...",
        "schema": "public",
    },
)
await conn.install()
await conn.health_check()

# PostgREST
rows = await conn.list_rows("posts",
                            filter={"published": True},
                            order="created_at.desc",
                            limit=10)
await conn.insert_row("posts", [{"title": "Hello"}])
await conn.update_row("posts", filter={"id": 1}, fields={"title": "Renamed"})
await conn.delete_row("posts", filter={"id": 1})
await conn.upsert("posts", [{"id": 1, "title": "Upserted"}], on_conflict="id")
answer = await conn.rpc("my_function", params={"x": 1})

# Auth Admin
users = await conn.list_users(page=1, per_page=50)
me = await conn.create_user(email="ada@ex.com",
                            password="hunter2",
                            user_metadata={"role": "admin"},
                            email_confirm=True)

# Storage
buckets = await conn.list_buckets()
await conn.upload_object("avatars", "me.png", b"\x89PNG...", content_type="image/png", upsert=True)
content = await conn.download_object("avatars", "me.png")
signed = await conn.create_signed_url("avatars", "me.png", expires_in=3600)

# Edge Functions
result = await conn.invoke_function("hello", {"name": "Ada"})
```

---

## Filter syntax (PostgREST)

| Python | URL |
|---|---|
| `{"id": 5}` | `id=eq.5` |
| `{"published": True}` | `published=eq.true` |
| `{"rating": {"gt": 4}}` | `rating=gt.4` |
| `{"id": [1, 2, 3]}` | `id=in.(1,2,3)` |
| `{"name": {"like": "%foo%"}}` | `name=like.%foo%` |

Supported single-key operators: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `like`,
`ilike`, `is`, `in`, `cs`, `cd`, `sl`, `sr`, `nxr`, `nxl`, `adj`, `ov`, `fts`,
`plfts`, `phfts`, `wfts`, `not`.
