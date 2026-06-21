# PlanetScale Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**PlanetScale** is a serverless MySQL-compatible database platform. Its REST API lives at `https://api.planetscale.com/v1` and exposes resources for managing organisations, databases, branches (Git-like schema branches), deploy requests (schema change proposals), backups, and database tokens (per-branch connection credentials). This connector — `PlanetScaleConnector` (`CONNECTOR_TYPE = "planetscale"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a PlanetScale account:

| Surface | Base path | Capability |
|---|---|---|
| Organizations | `/organizations` | List + read orgs the service token can see |
| Databases | `/organizations/{org}/databases` | List, get, create, delete databases |
| Branches | `/organizations/{org}/databases/{db}/branches` | List, get, create, delete branches |
| Deploy Requests | `/organizations/{org}/databases/{db}/deploy-requests` | List, get, create deploy requests |
| Backups | `/organizations/{org}/databases/{db}/branches/{br}/backups` | List backups for a branch |
| Database Tokens | `/organizations/{org}/databases/{db}/branches/{br}/passwords` | List branch connection credentials |

The connector normalises **databases** and **branches** into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts honouring `Retry-After`), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::PlanetScaleHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator for `PlanetScaleRateLimitError`-style 429 handling |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

PlanetScale REST API uses **service-token authentication** for server-to-server integrations. There is **no** OAuth dance and **no** token expiry.

### Credentials
- `service_token_id` — PlanetScale service token identifier (publicly identifies the token). Created in **Dashboard → Settings → Service tokens → Generate token**. install_field (type `text`, required).
- `service_token` — The secret value of the service token. PlanetScale shows this exactly once at creation. install_field (type `secret`, required).
- `default_organization` — Default org slug used when a method omits `organization`. install_field (type `text`, optional).
- `default_database` — Default database name used when a method omits `database`. install_field (type `text`, optional).

### Header contract — PlanetScale-specific gotcha

PlanetScale does **NOT** use `Bearer`. The Authorization header value is the literal `id:token` combo:

```
Authorization: <service_token_id>:<service_token>
Accept:        application/json
Content-Type:  application/json
```

Sending `Bearer <token>` returns 401. Sending only the token (no id) returns 401. The id and the secret together are the credential.

### Lifecycle
- `install()` validates `service_token_id` + `service_token` are non-empty. Does **not** call the API.
- `authorize()` — returns a synthetic `TokenInfo(access_token=f"{id}:{secret}", token_type="ServiceToken")` so the platform can persist the credential under the standard token surface.
- `health_check()` — `GET /organizations` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

PlanetScale **databases** and **branches** are the two normalised "document" surfaces — they carry the org/region/plan/state metadata callers index on.

### 4.1 Database → NormalizedDocument

| NormalizedDocument | PlanetScale JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{db['id']}"` | tenant-scoped |
| `source_id` | `db["id"]` (fallback `db["name"]`) | PlanetScale UUID or slug |
| `title` | `db["name"]` | |
| `content` | `"PlanetScale database: <name>\nPlan: ...\nRegion: ...\nState: ..."` | derived |
| `source` | `"planetscale.databases"` | |
| `created_at` | `db["created_at"]` (RFC 3339) | |
| `updated_at` | `db["updated_at"]` | |
| `metadata` | `{plan, region, state, kind: "planetscale.database"}` | |

### 4.2 Branch → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{branch['id']}"` |
| `source_id` | `branch["id"]` |
| `title` | `branch["name"]` |
| `content` | `"PlanetScale branch: <name>\nParent: ...\nProduction: ...\nReady: ..."` |
| `source` | `"planetscale.branches"` |
| `created_at` | `branch["created_at"]` |
| `metadata` | `{parent_branch, production, ready, kind: "planetscale.branch"}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/organizations` | Lightweight probe (lists at most a page of orgs). |
| `sync(since, full, kb_id)` | (lifecycle) | iterates orgs → databases → branches | Calls `ingest_document` per item. |
| `list_organizations()` | GET | `/organizations` | Page-paginated (PlanetScale `page` / `per_page`). |
| `get_organization(name)` | GET | `/organizations/{name}` | |
| `list_databases(organization?, page?, per_page?)` | GET | `/organizations/{org}/databases` | Org falls back to `default_organization`. |
| `get_database(organization?, name)` | GET | `/organizations/{org}/databases/{name}` | |
| `create_database(organization?, name, plan?, cluster_size?, region?)` | POST | `/organizations/{org}/databases` | |
| `delete_database(organization?, name)` | DELETE | `/organizations/{org}/databases/{name}` | |
| `list_branches(organization?, database?, page?, per_page?)` | GET | `/organizations/{org}/databases/{db}/branches` | |
| `get_branch(organization?, database?, name)` | GET | `/organizations/{org}/databases/{db}/branches/{name}` | |
| `create_branch(organization?, database?, name, parent_branch?, backup_id?)` | POST | `/organizations/{org}/databases/{db}/branches` | |
| `delete_branch(organization?, database?, name)` | DELETE | `/organizations/{org}/databases/{db}/branches/{name}` | |
| `list_deploy_requests(organization?, database?, state?, page?)` | GET | `/organizations/{org}/databases/{db}/deploy-requests` | |
| `get_deploy_request(organization?, database?, number)` | GET | `/organizations/{org}/databases/{db}/deploy-requests/{n}` | |
| `create_deploy_request(organization?, database?, branch, into_branch?, notes?)` | POST | `/organizations/{org}/databases/{db}/deploy-requests` | |
| `list_backups(organization?, database?, branch, page?)` | GET | `/organizations/{org}/databases/{db}/branches/{br}/backups` | |
| `list_database_tokens(organization?, database?, branch, page?)` | GET | `/organizations/{org}/databases/{db}/branches/{br}/passwords` | "Passwords" in API; "Database Tokens" in UI. |

Wire convention: PlanetScale uses **snake_case** in JSON (`created_at`, `parent_branch`, `cluster_size`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | PlanetScale meaning | Mapped to |
|---|---|---|
| 400 | Bad request (validation error) | `PlanetScaleBadRequestError` (raise) |
| 401 | Token id/secret mismatch or missing header | `PlanetScaleAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Service token lacks org/db scope | `PlanetScaleAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Org / database / branch not found | `PlanetScaleNotFoundError` (raise) |
| 409 | Conflict (e.g. branch already exists, db name taken) | `PlanetScaleConflictError` |
| 422 | Validation error (unprocessable entity) | `PlanetScaleBadRequestError` |
| 429 | Rate limited (`Retry-After` header honoured) | `PlanetScaleRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `PlanetScaleServerError` → retry with exponential backoff |

All in `exceptions.py` extending `PlanetScaleError`. Retry in `client/http_client.py::_request` honours `_MAX_RETRIES=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt` for 5xx, and the `Retry-After` header for 429 when present.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads `requirements.txt`):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed in the shared connector venv.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `service_token_id` | text | yes | install_field | Token identifier; sent as the left half of the Authorization header |
| `service_token` | secret | yes | install_field | Token secret; right half of the Authorization header |
| `default_organization` | text | no | install_field | Default `organization` for methods that accept it |
| `default_database` | text | no | install_field | Default `database` for methods that accept it |
| `base_url` | text | no | install_field (default `https://api.planetscale.com/v1`) | Override for testing |
| `rate_limit_per_min` | number | no | install_field (default 100) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["service_token_id", "service_token"]
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
| `helpers/normalizer.py` | Maps raw PlanetScale payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry helper, identifier validation, safe accessors. | (stdlib only) |
| `models.py` | Pydantic schemas for request bodies + provider dataclasses for typed callers. | `pydantic` |
| `exceptions.py` | `PlanetScaleError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `PlanetScaleConnector`. | `connector` |

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
