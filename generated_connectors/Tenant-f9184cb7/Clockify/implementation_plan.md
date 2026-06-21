# Clockify Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Clockify** is a time-tracking SaaS exposing a REST API at
`https://api.clockify.me/api/v1` plus a separate Reports API at
`https://reports.api.clockify.me/v1`. This connector — `ClockifyConnector`
(`CONNECTOR_TYPE = "clockify"`, `AUTH_TYPE = "api_key"`) — wraps the
operational surfaces a Shielva tenant typically needs:

| Surface | Base path | Capability |
|---|---|---|
| User | `/user` | Identity probe — used by `health_check()` |
| Workspaces | `/workspaces` | List every workspace the user belongs to |
| Projects | `/workspaces/{wid}/projects` | List + read + create projects |
| Tasks | `/workspaces/{wid}/projects/{pid}/tasks` | List tasks per project |
| Clients | `/workspaces/{wid}/clients` | List + create clients |
| Tags | `/workspaces/{wid}/tags` | List tags |
| Users | `/workspaces/{wid}/users` | List workspace members |
| Time entries | `/workspaces/{wid}/time-entries` + `/workspaces/{wid}/user/{uid}/time-entries` | Full CRUD + start/stop timer |
| Reports | `/workspaces/{wid}/reports/summary` (reports host) | Summary report |

The connector normalises time entries into `NormalizedDocument`
(id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def`
methods per user-requested operation (OCP), retries 429/5xx with
exponential backoff + jitter (3 attempts), and never embeds raw HTTP in
`connector.py` — all I/O is delegated to
`client/http_client.py::ClockifyHTTPClient`.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Local dataclass / model definitions; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator parity with other Shielva connectors |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`,
`respx`.

## 3. Auth Flow

Clockify uses **API key authentication** only — no OAuth, no token
refresh, no expiry.

### Credentials
- `api_key` — generated at Clockify → **Profile Settings → API**.
  Stored as install_field (type `secret`, required).
- `default_workspace_id` — optional; when set, `sync()` uses it without
  calling `/workspaces`.
- `base_url` — optional override for the main API host.
- `reports_base_url` — optional override for the Reports API host
  (different from the main host).
- `rate_limit_per_min` — optional client-side soft cap.

### Header contract
Every request to `https://api.clockify.me/api/v1/*` and
`https://reports.api.clockify.me/v1/*`:

```
X-Api-Key:    <api_key>
Content-Type: application/json
Accept:       application/json
```

### Lifecycle
- `install()` validates `api_key` is non-empty; does **not** call the API.
- `authorize()` synthesises a `TokenInfo(token_type="ApiKey")` so the SDK
  session bookkeeping is satisfied; there is no token exchange.
- `health_check()` — `GET /user` as a lightweight probe.
- `ensure_token()` — N/A.

## 4. Data Model

### 4.1 Time entry → NormalizedDocument

| NormalizedDocument | Clockify JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{entry['id']}"` | tenant-scoped per spec |
| `source_id` | `entry["id"]` | Clockify entry UUID |
| `title` | `entry["description"] or f"Time entry {id}"` | |
| `content` | description + project + interval summary | |
| `author` | `entry["userId"]` | |
| `metadata` | `{workspace_id, project_id, billable, tag_ids, start, end, duration}` | |

### 4.2 Project → ProjectModel (local)

Plain dataclass shim used by callers that want a typed view. Direct
`Dict[str, Any]` is the connector boundary.

### 4.3 Other resources (workspaces, clients, tags, tasks, users)

Returned as raw `Dict[str, Any]` / `List[Dict]` — no normalisation
required since these are reference data, not knowledge-base documents.

## 5. Key API Endpoints & Methods

Every method MUST exist as a standalone public `async def` in
`connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/user` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates time entries | Calls `ingest_document`. |
| `get_current_user()` | GET | `/user` | Authenticated user profile. |
| `list_workspaces()` | GET | `/workspaces` | Every workspace the user belongs to. |
| `list_projects(workspace_id, archived, name, page, page_size)` | GET | `/workspaces/{wid}/projects` | Supports `archived`, `name`, paging. |
| `get_project(workspace_id, project_id)` | GET | `/workspaces/{wid}/projects/{pid}` | Single project. |
| `create_project(workspace_id, name, ...)` | POST | `/workspaces/{wid}/projects` | `billable` + `hourlyRate` supported. |
| `list_tasks(workspace_id, project_id, ...)` | GET | `/workspaces/{wid}/projects/{pid}/tasks` | Default status `ACTIVE`. |
| `list_time_entries(workspace_id, user_id, ...)` | GET | `/workspaces/{wid}/user/{uid}/time-entries` | Supports `start`, `end`, `project`. |
| `get_time_entry(workspace_id, entry_id)` | GET | `/workspaces/{wid}/time-entries/{eid}` | Single entry by id. |
| `start_time_entry(workspace_id, ...)` | POST | `/workspaces/{wid}/time-entries` | `end` omitted → running timer. |
| `stop_time_entry(workspace_id, user_id, end)` | PATCH | `/workspaces/{wid}/user/{uid}/time-entries` | Body: `{end}`. |
| `update_time_entry(workspace_id, entry_id, fields)` | PUT | `/workspaces/{wid}/time-entries/{eid}` | Generic patch body. |
| `delete_time_entry(workspace_id, entry_id)` | DELETE | `/workspaces/{wid}/time-entries/{eid}` | |
| `list_tags(workspace_id, ...)` | GET | `/workspaces/{wid}/tags` | Supports paging + archived. |
| `list_clients(workspace_id, ...)` | GET | `/workspaces/{wid}/clients` | |
| `create_client(workspace_id, name, ...)` | POST | `/workspaces/{wid}/clients` | |
| `list_users(workspace_id, ...)` | GET | `/workspaces/{wid}/users` | List workspace members. |
| `summary_report(workspace_id, ...)` | POST | `{reports_base}/workspaces/{wid}/reports/summary` | Different host. |

Wire convention: Clockify uses **camelCase** in JSON (`workspaceId`,
`projectId`, `hourlyRate`, `tagIds`). The connector boundary accepts
Python snake_case params and translates to camelCase in payload
construction.

## 6. Error Handling

| HTTP | Clockify meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `ClockifyError` (raise) |
| 401 | API key missing / invalid | `ClockifyAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` |
| 403 | Forbidden — key lacks permissions | `ClockifyAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `ClockifyNotFound` (raise) |
| 429 | Rate limited | `ClockifyRateLimitError` → exponential backoff retry, then `DEGRADED + CONNECTED` |
| 5xx | Provider outage | `ClockifyError` / `ClockifyNetworkError` → retry with exponential backoff |

All in `exceptions.py` extending `ClockifyError`. Retry in
`client/http_client.py::_request` honours an inner retry loop
(`_INNER_RETRIES=2`) with exponential backoff + jitter; the connector
layer wraps each call in `helpers/utils.py::with_retry` for an outer
retry budget.

`_STATUS_MAP` on the connector class:

```python
_STATUS_MAP = {
    401: ("DEGRADED",  "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 7. Dependencies

Packages to install in the connector's venv (`install_deps` reads this
section):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx
are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `X-Api-Key` header value |
| `default_workspace_id` | text | no | install_field | Used by `sync()` when blank caller-side |
| `base_url` | text | no | install_field (default `https://api.clockify.me/api/v1`) | |
| `reports_base_url` | text | no | install_field (default `https://reports.api.clockify.me/v1`) | Different host |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_key"]
_STATUS_MAP = {
    401: ("DEGRADED",  "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Clockify payloads → `NormalizedDocument` and local dataclasses. | `shared.base_connector.NormalizedDocument`, `models` |
| `helpers/utils.py` | Retry helper, paged-params builder. | (stdlib + `exceptions`) |
| `models.py` | Local dataclasses (`ProjectModel`, `TimeEntryModel`, `ClientModel`, …). | (stdlib) |
| `exceptions.py` | `ClockifyError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `ClockifyConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying `BaseConnector` ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, paging) as composable helpers ✓
10. Error mapping in `exceptions.py`; `connector.py` catches custom exceptions only ✓

**Score: 10/10.**
