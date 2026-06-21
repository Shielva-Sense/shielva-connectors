# n8n Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**n8n** is a self-hostable / cloud workflow-automation platform exposing a public REST API at `${instance_url}/api/v1`. This connector — `N8nConnector` (`CONNECTOR_TYPE = "n8n"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from an n8n instance:

| Surface | Base path | Capability |
|---|---|---|
| Workflows | `/workflows` | List, get, create, update, delete, activate, deactivate, transfer |
| Executions | `/executions` | List, get, delete past runs |
| Credentials | `/credentials` | List, get, create, delete provider credentials |
| Tags | `/tags` | List + create tags for workflow grouping |
| Users | `/users` | List instance users (community/enterprise) |
| Variables | `/variables` | List instance-level environment variables |

The connector exposes 16 standalone `async def` methods (OCP), routes all HTTP through `client/http_client.py::N8nHTTPClient` (SOC), normalises workflows + executions into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`) when `sync()` is invoked, and retries 429 / 5xx with exponential backoff and jitter (max 3).

**Per-tenant base URL.** Unlike a fixed-API provider, n8n is *tenant-hosted* — the Shielva tenant runs their own Cloud or self-hosted n8n instance. The `instance_url` install field is therefore mandatory; `${instance_url}/api/v1` is computed at `__init__` and is the *only* base URL the HTTP client ever talks to. Multi-tenant isolation is enforced by:

1. One `N8nConnector` instance per `(tenant_id, connector_id)`.
2. `self.base_url` derived from per-tenant `instance_url` — never a class-level constant.
3. Every `NormalizedDocument.id` is `f"{tenant_id}_{source_id}"`.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator wrapping rate-limit handling |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

n8n REST API uses **API key authentication** for server-to-server integrations. There is no OAuth or token-refresh dance.

### Credentials
- `api_key` — Generated in **n8n → Settings → n8n API → Create an API key**. Stored as install_field (type `secret`, required). The key is sent verbatim in the `X-N8N-API-KEY` header on every request.
- `instance_url` — Tenant-hosted n8n base URL, e.g. `https://yourorg.app.n8n.cloud` or `https://n8n.internal.acme.com`. install_field (type `text`, required).
- `rate_limit_per_min` — Client-side soft cap (default 60). install_field (type `number`, optional).

### Header contract
Every outbound request to `${instance_url}/api/v1/*`:

```
X-N8N-API-KEY: <api_key>
Accept:        application/json
Content-Type:  application/json
```

### Lifecycle
- `install()` validates `instance_url` + `api_key` are non-empty. Does **not** call the API.
- `authorize()` — falls through to `install()` (api_key flow has no exchange).
- `health_check()` — `GET /workflows?limit=1` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Workflow → NormalizedDocument

| NormalizedDocument | n8n JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{wf['id']}"` | tenant-scoped |
| `source_id` | `wf["id"]` | n8n workflow UUID/numeric id |
| `title` | `wf["name"]` | Workflow display name |
| `content` | concat name + tag names + node-type summary | for embedding |
| `source` | `"n8n.workflow"` | |
| `created_at` | `wf["createdAt"]` | RFC 3339 |
| `updated_at` | `wf["updatedAt"]` | |
| `metadata` | `{active, tags, node_count, has_trigger, kind: "n8n.workflow"}` | |

### 4.2 Execution → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{ex['id']}"` |
| `source_id` | `ex["id"]` |
| `title` | `f"Execution {ex['id']}"` |
| `content` | `f"{status} workflow={workflowId}"` |
| `source` | `"n8n.execution"` |
| `created_at` | `ex["startedAt"]` |
| `metadata` | `{workflow_id, status, finished, started_at, stopped_at, kind: "n8n.execution"}` |

## 5. Key API Endpoints & Methods

Every method listed below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/workflows?limit=1` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates workflows + executions | Calls `ingest_document` per item. |
| `list_workflows(*, active, tags, name, project_id, exclude_pinned_data, limit, cursor)` | GET | `/workflows` | Cursor pagination + filters. |
| `get_workflow(workflow_id, *, exclude_pinned_data=False)` | GET | `/workflows/{id}` | |
| `create_workflow(name, nodes, connections, settings, static_data)` | POST | `/workflows` | |
| `update_workflow(workflow_id, *, name, nodes, connections, settings, active)` | PUT | `/workflows/{id}` | |
| `delete_workflow(workflow_id)` | DELETE | `/workflows/{id}` | |
| `activate_workflow(workflow_id)` | POST | `/workflows/{id}/activate` | |
| `deactivate_workflow(workflow_id)` | POST | `/workflows/{id}/deactivate` | |
| `transfer_workflow(workflow_id, destination_project_id)` | PUT | `/workflows/{id}/transfer` | Enterprise. |
| `list_executions(*, workflow_id, status, limit, cursor, include_data)` | GET | `/executions` | Cursor pagination. |
| `get_execution(execution_id, *, include_data=False)` | GET | `/executions/{id}` | |
| `delete_execution(execution_id)` | DELETE | `/executions/{id}` | |
| `list_credentials(*, limit, cursor)` | GET | `/credentials` | |
| `get_credential(credential_id)` | GET | `/credentials/{id}` | |
| `create_credential(name, type, data)` | POST | `/credentials` | |
| `delete_credential(credential_id)` | DELETE | `/credentials/{id}` | |
| `list_tags(*, limit, cursor)` | GET | `/tags` | |
| `create_tag(name)` | POST | `/tags` | |
| `list_users(*, limit, cursor)` | GET | `/users` | Enterprise / community-edition. |
| `list_variables(*, limit, cursor)` | GET | `/variables` | Enterprise. |

Wire convention: n8n uses **camelCase** in JSON (`workflowId`, `nodes`, `connections`, `excludePinnedData`). The connector boundary accepts snake_case kwargs and translates to camelCase query params / body fields in `helpers/utils.py`. Response payloads are returned as-is in `Dict[str, Any]`.

## 6. Error Handling

| HTTP | n8n meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `N8nBadRequestError` (raise) |
| 401 | API key invalid / missing header | `N8nAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` |
| 403 | Forbidden (key lacks permissions / community-only feature) | `N8nAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `N8nNotFound` (raise) |
| 409 | Conflict (e.g. duplicate tag) | `N8nConflictError` |
| 429 | Rate limited (`Retry-After` honoured when present) | `N8nRateLimitError` → retry up to 3, then `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `N8nServerError` → retry with exponential backoff |
| transport | `httpx.TimeoutException` / `NetworkError` | `N8nNetworkError` after exhausted retries |

All exceptions live in `exceptions.py` and extend `N8nError`. The retry loop in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `min(RETRY_BASE_DELAY_S * BACKOFF_FACTOR ** attempt + jitter, MAX_RETRY_DELAY_S)`. 429 responses honour the `Retry-After` header when provided.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `instance_url` | text | yes | install_field | Tenant-hosted base URL, e.g. `https://acme.app.n8n.cloud` |
| `api_key` | secret | yes | install_field | `X-N8N-API-KEY` header value |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |
| `timeout_s` | number | no | install_field (default 30) | Per-request httpx timeout |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["instance_url", "api_key"]
_STATUS_MAP = {
    401: ("DEGRADED",  "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing, no header strings.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds the `X-N8N-API-KEY` header, retries 429/5xx, raises typed exceptions. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw n8n payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | snake_case → camelCase param builders for `/workflows` + `/executions` lists. | (stdlib only) |
| `models.py` | Pydantic schemas with camelCase aliases for request bodies. | `pydantic` |
| `exceptions.py` | `N8nError` hierarchy. | (stdlib) |
| `__init__.py` | Self-bootstraps `sys.path` (insert connector root + monorepo core) then re-exports `N8nConnector`. | `connector` |

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
