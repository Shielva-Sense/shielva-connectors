# Supabase Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Supabase** is an open-source Firebase alternative — a managed Postgres database plus PostgREST, GoTrue (Auth), Storage, Realtime, and Edge Functions, exposed at `https://{project_ref}.supabase.co`. This connector — `SupabaseConnector` (`CONNECTOR_TYPE = "supabase"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs:

| Surface | Base path | Capability |
|---|---|---|
| PostgREST (REST) | `/rest/v1/{table}` | CRUD on tables with PostgREST filter syntax |
| Auth Admin (GoTrue) | `/auth/v1/admin/users` | List, get, create, update, delete users |
| Storage | `/storage/v1/{bucket,object}` | Buckets + objects (upload/download/delete) |
| Edge Functions | `/functions/v1/{name}` | Invoke Deno-runtime serverless functions |
| RPC | `/rest/v1/rpc/{fn}` | Call SQL stored procedures |

The connector authenticates with the **service-role API key**, sent as BOTH:
- `Authorization: Bearer <service_role_key>`
- `apikey: <service_role_key>`

`Content-Profile` / `Accept-Profile` headers carry the Postgres schema (default `public`).

The connector normalises table rows into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces every user-named operation as a standalone `async def` method (OCP), and retries 429/5xx with exponential backoff (3 attempts). All raw HTTP lives in `client/http_client.py::SupabaseHTTPClient`; `connector.py` orchestrates only (SOC).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Local request schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

The Supabase Python SDK (`supabase-py`) is intentionally NOT used — it pulls a synchronous `postgrest-py` plus `gotrue-py` that would force a thread pool wrap and obscure SOC. A thin async httpx layer is smaller and easier to audit.

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Supabase REST + Auth Admin + Storage use **service-role API key authentication** for server-to-server.

### Credentials
- `project_url` — Full project URL, e.g. `https://abcdwxyz.supabase.co`. install_field (type `string`, required). Accept short form `project_ref` legacy alias.
- `service_role_key` — Long-lived `eyJ...` JWT issued by Supabase. install_field (type `secret`, required). Bypasses Row-Level Security; treat as a master key.
- `schema` — Postgres schema name for PostgREST calls (default `public`). install_field (type `string`, optional).
- `rate_limit_per_min` — Client-side soft cap (default 100). install_field (type `number`, optional).

### Header contract
Every request to `https://{project_ref}.supabase.co/*`:

```
Authorization: Bearer <service_role_key>
apikey:        <service_role_key>
Content-Type:  application/json           (when body present)
Accept:        application/json           (REST + Auth)
Accept-Profile: <schema>                  (PostgREST only)
Content-Profile: <schema>                 (PostgREST writes)
```

### Lifecycle
- `install()` validates `project_url` + `service_role_key` are non-empty. Does **not** call the API.
- `authorize()` — no OAuth exchange. Probes `/auth/v1/settings` (or `/rest/v1/`) to confirm the key is accepted.
- `health_check()` — `GET /auth/v1/settings` as a lightweight, RLS-free probe that requires only a valid `apikey` header. Falls back to `/rest/v1/` on 404.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Row → NormalizedDocument

| NormalizedDocument | Supabase JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{table}:{row.id}"` | tenant-scoped |
| `source_id` | `f"{table}:{row.id}"` or sha256(row body)[:16] | stable when id absent |
| `title` | `row.title` ∨ `row.name` ∨ `row.subject` ∨ source_id | |
| `content` | `row.content` ∨ `row.body` ∨ `row.description` ∨ `json.dumps(row)` | |
| `source_url` | None (PostgREST rows have no URL) | |
| `author` | `row.author` ∨ `row.user_id` ∨ `""` | |
| `created_at` | `row.created_at` (RFC 3339 → datetime) | |
| `updated_at` | `row.updated_at` ∨ `created_at` | |
| `source` | `"supabase"` | |
| `metadata` | `{"table": table, "raw": row}` | |

### 4.2 User → NormalizedDocument

GoTrue user → `id = f"{tenant_id}_users:{user.id}"`, `title = user.email`, `content = json.dumps(user_metadata)`, `metadata.kind = "supabase.user"`.

### 4.3 Storage object → NormalizedDocument

Storage object metadata → `id = f"{tenant_id}_{bucket}/{path}"`, `title = path`, `content_type = mime_type`, `source_url = signed_url`, `metadata = {bucket, size, mimetype}`.

## 5. Key API Endpoints & Methods

Every method listed must exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `authorize(auth_code, state)` | (lifecycle) | n/a | No-op; runs health_check. |
| `health_check()` | GET | `/auth/v1/settings` (fallback `/rest/v1/`) | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | n/a | No-op; orchestrator drives per-table ingestion via `select` + `normalize_row`. |
| `list_rows(table, columns, filter, order, limit)` | GET | `/rest/v1/{table}?select=...` | PostgREST filter dict. Alias: `select`. |
| `get_row(table, row_id)` | GET | `/rest/v1/{table}?id=eq.{id}&limit=1` | Single row by id. |
| `insert_row(table, rows, returning)` | POST | `/rest/v1/{table}` | Prefer: return=representation. Alias: `insert`. |
| `update_row(table, filter, fields)` | PATCH | `/rest/v1/{table}?<filter>` | Alias: `update`. |
| `delete_row(table, filter)` | DELETE | `/rest/v1/{table}?<filter>` | Alias: `delete`. |
| `upsert(table, rows, on_conflict)` | POST | `/rest/v1/{table}` | Prefer: resolution=merge-duplicates. |
| `rpc(function_name, params)` | POST | `/rest/v1/rpc/{fn}` | Stored function invocation. |
| `list_users(page, per_page)` | GET | `/auth/v1/admin/users` | Paginated. |
| `get_user(user_id)` | GET | `/auth/v1/admin/users/{id}` | |
| `create_user(email, password, user_metadata)` | POST | `/auth/v1/admin/users` | Service-role only. |
| `update_user(user_id, attrs)` | PUT | `/auth/v1/admin/users/{id}` | |
| `delete_user(user_id)` | DELETE | `/auth/v1/admin/users/{id}` | |
| `list_buckets()` | GET | `/storage/v1/bucket` | |
| `list_objects(bucket, prefix, limit, offset)` | POST | `/storage/v1/object/list/{bucket}` | |
| `upload_object(bucket, path, content, content_type, upsert)` | POST | `/storage/v1/object/{bucket}/{path}` | |
| `download_object(bucket, path)` | GET | `/storage/v1/object/{bucket}/{path}` | Returns raw bytes. |
| `delete_object(bucket, path)` | DELETE | `/storage/v1/object/{bucket}/{path}` | |
| `invoke_function(name, payload)` | POST | `/functions/v1/{name}` | Edge Function invocation. |

Aliases (`select`, `insert`, `update`, `delete`) preserved for the canonical CRUD names listed in `plan_steps.json` so the runtime catalogue contains both naming styles.

## 6. Error Handling

| HTTP | Supabase meaning | Mapped to |
|---|---|---|
| 400 | Bad request (e.g. malformed filter) | `SupabaseBadRequestError` |
| 401 | API key invalid / missing | `SupabaseAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (RLS denial / wrong key) | `SupabaseAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Table / user / object not found | `SupabaseNotFoundError` |
| 409 | Conflict (duplicate key, unique violation) | `SupabaseConflictError` |
| 422 | Postgres constraint violation | `SupabaseBadRequestError` |
| 429 | Rate-limited (Cloudflare / Supabase tier) | `SupabaseRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `SupabaseServerError` → retry with exponential backoff |

All in `exceptions.py` extending `SupabaseError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `min(2 ** attempt, 8)`s for 5xx, fixed 5s for 429.

Back-compat alias: `SupabaseNotFound = SupabaseNotFoundError`, `SupabaseNetworkError = SupabaseServerError`.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27,<1.0
```

(pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `project_url` | string | yes | install_field | Full project URL (`https://abcdwxyz.supabase.co`). Accepts legacy `project_ref`. |
| `service_role_key` | secret | yes | install_field | Long-lived JWT; treat as master key. |
| `schema` | string | no (default `public`) | install_field | PostgREST schema. |
| `rate_limit_per_min` | number | no (default 100) | install_field | Client-side soft cap. |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["project_url", "service_role_key"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Supabase rows / users / objects → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | PostgREST filter translation, retry helper. | `httpx`, `structlog`, `exceptions` |
| `models.py` | Pydantic / dataclass schemas for request bodies (no React, no fetch). | `pydantic` |
| `exceptions.py` | `SupabaseError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `SupabaseConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, pagination) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
