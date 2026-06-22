# Grafana Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Grafana** is an open-source observability / dashboarding platform with a REST HTTP API. This connector — `GrafanaConnector` (`CONNECTOR_TYPE = "grafana"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Grafana stack:

| Surface | Base path | Capability |
|---|---|---|
| Health | `/api/health` | Server reachability + database state probe |
| Org | `/api/org` | Current organization for the auth token |
| Dashboards | `/api/dashboards` + `/api/search` | List, get, create, update, delete dashboards |
| Folders | `/api/folders` | List + create folders |
| Datasources | `/api/datasources` + `/api/ds/query` | List, create datasources; run datasource queries |
| Alert Rules | `/api/v1/provisioning/alert-rules` | List provisioned alert rules |
| Users | `/api/users` | Paginated user list |
| Teams | `/api/teams/search` | Search teams |
| Annotations | `/api/annotations` | List + create annotations (extension surface) |

The base URL is **tenant-specific** — `https://<stack>.grafana.net` (Grafana Cloud) or `http://localhost:3000` (self-hosted). The connector requires `instance_url` as an install field; it is **never hardcoded**.

The connector normalises Grafana dashboards into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff that honours `Retry-After`, and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::GrafanaHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Local dataclass-style models for typed wire shapes; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

The Grafana REST API does not have a first-party async Python SDK that is feature-complete; we wrap the REST surface directly with `httpx` for full control over retry, headers, and tenant routing.

## 3. Auth Flow

Grafana REST API uses **Service Account token authentication** for server-to-server integrations (a long-lived API key in modern Grafana parlance).

### Credentials
- `instance_url` — the tenant's Grafana stack URL (e.g. `https://myorg.grafana.net` or `http://localhost:3000`). install_field (`type: string`, required).
- `service_account_token` — Grafana service account token (`glsa_...`). Create at **Administration → Service Accounts → Add Service Account → Add Token**. install_field (`type: secret`, required).
- `org_id` — optional Grafana org id (defaults to 1). install_field (`type: number`, optional).

### Header contract
Every request to `{instance_url}/api/*`:

```
Authorization: Bearer <service_account_token>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `instance_url` and `service_account_token` are non-empty. Does **not** call the API.
- `authorize()` — wraps the token in a `TokenInfo(token_type="Bearer")` and calls `set_token`; no OAuth exchange.
- `health_check()` — `GET /api/health` as a lightweight probe (no auth required, but token still sent).
- `ensure_token()` — N/A (Service Account tokens are non-expiring until revoked).

## 4. Data Model

### 4.1 Dashboard → NormalizedDocument

| NormalizedDocument | Grafana JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{uid}"` | tenant-scoped |
| `source_id` | `search_hit["uid"]` | Grafana dashboard UID |
| `title` | `search_hit["title"]` | |
| `content` | concat title + folder + tags + panel-titles | |
| `source` | `"grafana"` | |
| `source_url` | `f"{instance_url}{search_hit['url']}"` | |
| `created_at` | `full_dashboard["meta"]["created"]` | RFC 3339 |
| `updated_at` | `full_dashboard["meta"]["updated"]` | |
| `metadata` | `{tags, folder_title, folder_uid, panels, type}` | |
| `tenant_id` | passed-in `tenant_id` | |
| `connector_id` | passed-in `connector_id` | |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `authorize()` | (lifecycle) | n/a | Wrap token in `TokenInfo`, persist via `set_token`. |
| `health_check()` | GET | `/api/health` | Lightweight server probe; reports database state. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates dashboards | Calls `ingest_document`. |
| `get_org()` | GET | `/api/org` | Current organization. |
| `list_dashboards(limit, page, query, tag, folder_uids)` | GET | `/api/search?type=dash-db` | Search-style listing with filters. |
| `get_dashboard(uid)` | GET | `/api/dashboards/uid/{uid}` | Full dashboard JSON. |
| `create_dashboard(dashboard, folder_uid, overwrite)` | POST | `/api/dashboards/db` | Create or upsert. |
| `delete_dashboard(uid)` | DELETE | `/api/dashboards/uid/{uid}` | Remove. |
| `list_folders(limit, page)` | GET | `/api/folders` | Folders. |
| `create_folder(title, uid)` | POST | `/api/folders` | New folder. |
| `list_datasources()` | GET | `/api/datasources` | Configured datasources. |
| `create_datasource(name, type, url, ...)` | POST | `/api/datasources` | Register datasource. |
| `query_datasource(datasource_id, queries, from_time, to_time)` | POST | `/api/ds/query` | Run datasource queries. |
| `list_alert_rules(limit)` | GET | `/api/v1/provisioning/alert-rules` | Provisioned alerts. |
| `list_users(perpage, page)` | GET | `/api/users` | Users. |
| `list_teams(perpage, page, query)` | GET | `/api/teams/search` | Teams. |
| `get_dashboard_doc(uid)` | (helper) | combines search + full | Returns a `NormalizedDocument`. |

Wire convention: Grafana uses **camelCase** in JSON (`folderUid`, `isDefault`, `jsonData`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Grafana meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `GrafanaAPIError` (raise) |
| 401 | Token invalid / revoked | `GrafanaAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Token lacks role/permission | `GrafanaAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Resource not found | `GrafanaNotFound` (raise) |
| 429 | Rate limited (honour `Retry-After`) | `GrafanaRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `GrafanaAPIError` → retry with exponential backoff |

All in `exceptions.py` extending `GrafanaError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `min(1.0 * 2 ** attempt, 32)` for 5xx, honours `Retry-After` header on 429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
# All required runtime deps (httpx, pydantic, structlog) are pre-installed in the shared venv.
# No additional packages required for the Grafana REST surface.
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `instance_url` | string | yes | install_field | Tenant-specific Grafana base URL |
| `service_account_token` | secret | yes | install_field | `Bearer` token value |
| `org_id` | number | no | install_field (default 1) | Grafana org id |
| `rate_limit_per_min` | number | no | install_field (default 300) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["instance_url", "service_account_token"]
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
| `helpers/normalizer.py` | Maps raw Grafana payloads → `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`). | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Caller-side retry wrapper around custom exceptions. | (stdlib only) |
| `models.py` | Dataclass-style local response shims (Org, Dashboard, Folder, ...). | (stdlib only) |
| `exceptions.py` | `GrafanaError` hierarchy (Auth, NotFound, RateLimit, Network, APIError). | (stdlib) |
| `__init__.py` | Self-bootstrap `sys.path` + re-export `GrafanaConnector`. | `connector` |

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
