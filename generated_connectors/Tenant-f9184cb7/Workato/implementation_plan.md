# Workato Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Workato** is an enterprise iPaaS / automation platform exposing a REST API at
`https://www.workato.com/api` (US) or `https://app.eu.workato.com/api` (EU). This
connector — `WorkatoConnector` (`CONNECTOR_TYPE = "workato"`, `AUTH_TYPE = "api_key"`)
— wraps the operational surfaces a Shielva tenant typically needs from a
Workato customer workspace:

| Surface | Base path | Capability |
|---|---|---|
| Recipes | `/api/recipes` | List, get, start, stop integration recipes |
| Connections | `/api/connections` | List + read connector connections |
| Folders | `/api/folders` | Project / folder navigation |
| Jobs | `/api/jobs` | Recipe job runs (audit + retry surface) |
| Lookup Tables | `/api/lookup_tables` | Configurable runtime data |
| Tags | `/api/tags` | Resource tagging |
| Users | `/api/users` | Workspace users |
| On-Prem Agents | `/api/on_prem_agents` | OPA fleet inventory |
| Customers | `/api/managed_users` | White-label customer accounts (embedded) |

The connector normalises **recipes**, **connections**, and **jobs** into
`NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces every
user-requested operation as a standalone `async def` (OCP), and routes all
HTTP through `client/http_client.py::WorkatoHTTPClient` (SOC).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator helper for transient errors |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Workato REST API uses **API token authentication** (Bearer) for
server-to-server integrations.

### Credentials
- `api_token` — Workato API token created in **Workspace Admin → API Clients →
  Generate Token**. Stored as install_field (type `secret`, required).
- `region` — `"us"` (default) or `"eu"`; selects the base URL. install_field
  (type `string`, optional, default `"us"`).
- `base_url` — Explicit override. install_field (type `string`, optional). When
  set, takes precedence over `region`.

### Header contract
Every request to `https://www.workato.com/api/*`:

```
Authorization: Bearer <api_token>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `api_token` is non-empty. Does **not** call the API.
- `authorize()` — NOT implemented (API-token flow has no exchange). Returns an
  empty TokenInfo wrapping `api_token` for ABI compatibility.
- `health_check()` — `GET /users/me` as a lightweight probe.
- `ensure_token()` — N/A (no token refresh lifecycle).

### Region → base URL

| `region` | Base URL |
|---|---|
| `us` (default) | `https://www.workato.com/api` |
| `eu` | `https://app.eu.workato.com/api` |
| custom | from `base_url` install_field |

## 4. Data Model

### 4.1 Recipe → NormalizedDocument

| NormalizedDocument | Workato JSON | Notes |
|---|---|---|
| `id` | `f"{connector_id}_{recipe['id']}"` | tenant-scoped via connector_id |
| `source_id` | `str(recipe["id"])` | Workato recipe int ID |
| `title` | `recipe["name"]` | |
| `content` | `recipe["description"]` or concat of name + status + folder | |
| `content_type` | `"text"` | |
| `source_url` | derived: `{base_url}/recipes/{id}` | |
| `created_at` | `recipe["created_at"]` | RFC 3339 |
| `updated_at` | `recipe["updated_at"]` | |
| `metadata` | `{running, job_succeeded_count, job_failed_count, folder_id, version_no, kind: "workato.recipe"}` | |

### 4.2 Connection → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{connector_id}_{conn['id']}"` |
| `source_id` | `str(conn["id"])` |
| `title` | `conn["name"]` |
| `content` | concat of provider + authorization_status |
| `created_at` | `conn["created_at"]` |
| `metadata` | `{provider, application, authorization_status, folder_id, kind: "workato.connection"}` |

### 4.3 Job → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{connector_id}_{job['id']}"` |
| `source_id` | `str(job["id"])` |
| `title` | `f"Job {job['id']} — {job.get('flow_run_id', '')}"` |
| `content` | `job.get("error", "") or job.get("status", "")` |
| `created_at` | `job.get("started_at") or job.get("created_at")` |
| `metadata` | `{status, recipe_id, completed_at, started_at, kind: "workato.job"}` |

## 5. Key API Endpoints & Methods

Every method below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate `api_token`; init HTTP client. |
| `health_check()` | GET | `/users/me` | Lightweight probe; auth+connectivity. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates recipes + connections + jobs | Calls `ingest_document` per item. |
| `list_recipes(page=1, per_page=100, folder_id=None, order=None)` | GET | `/recipes` | Paginated list. |
| `get_recipe(recipe_id)` | GET | `/recipes/{id}` | Single recipe. |
| `start_recipe(recipe_id)` | PUT | `/recipes/{id}/start` | Toggle running ON. |
| `stop_recipe(recipe_id)` | PUT | `/recipes/{id}/stop` | Toggle running OFF. |
| `list_connections(page=1, per_page=100)` | GET | `/connections` | Paginated. |
| `get_connection(connection_id)` | GET | `/connections/{id}` | |
| `create_connection(payload)` | POST | `/connections` | Body: connection object. |
| `list_folders(page=1, per_page=100, parent_id=None)` | GET | `/folders` | |
| `list_jobs(recipe_id, page=1, per_page=100, status=None)` | GET | `/recipes/{id}/jobs` | Recipe-scoped. |
| `get_job(recipe_id, job_id)` | GET | `/recipes/{id}/jobs/{job_id}` | |
| `list_lookup_tables(page=1, per_page=100)` | GET | `/lookup_tables` | |
| `list_tags(page=1, per_page=100)` | GET | `/tags` | |
| `list_users(page=1, per_page=100)` | GET | `/users` | Workspace users. |
| `list_on_prem_agents(page=1, per_page=100)` | GET | `/on_prem_agents` | OPA inventory. |
| `list_customers(page=1, per_page=100)` | GET | `/managed_users` | White-label customers. |

Wire convention: Workato uses **snake_case** in JSON (`created_at`,
`running`, `folder_id`). The connector boundary accepts/returns these as-is
in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Workato meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `WorkatoBadRequestError` (raise) |
| 401 | Token invalid / missing header | `WorkatoAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (token lacks workspace role) | `WorkatoAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `WorkatoNotFoundError` (raise) |
| 409 | Conflict (e.g. cannot start already-running recipe) | `WorkatoConflictError` |
| 429 | Rate limited | `WorkatoRateLimitError` → `ConnectorHealth.DEGRADED`; retry with exponential backoff |
| 5xx | Provider outage | `WorkatoServerError` → retry with exponential backoff |

All in `exceptions.py` extending `WorkatoError`. Retry in
`client/http_client.py::_request` honours `max_retries=3`, exponential backoff
`_BACKOFF_BASE * 2 ** attempt` for 429 and 5xx.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(`httpx`, `pydantic`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock`,
`respx` are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_token` | secret | yes | install_field | Bearer token, sent as `Authorization: Bearer <api_token>` |
| `region` | string | no | install_field | `"us"` (default) or `"eu"`; selects the base URL |
| `base_url` | string | no | install_field | Override the resolved base URL (rarely needed) |
| `rate_limit_per_min` | number | no | install_field (default 100) | Client-side soft cap |
| `timeout_s` | number | no | install_field (default 30) | Per-request httpx timeout |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_token"]
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
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions on HTTP error. Owns region→URL resolution. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Workato payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Async `with_retry` and `safe_get` helpers. | (stdlib only) |
| `models.py` | Pydantic schemas with snake_case fields for request/response bodies. | `pydantic` |
| `exceptions.py` | `WorkatoError` hierarchy. | (stdlib) |
| `__init__.py` | self-bootstrap `sys.path` so the module imports as both `workato_connector` and standalone. Re-export `WorkatoConnector`. | `connector` |

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
