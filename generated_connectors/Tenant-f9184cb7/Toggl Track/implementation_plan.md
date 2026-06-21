# Toggl Track Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Toggl Track** is a time-tracking SaaS exposing a REST API under `https://api.track.toggl.com/api/v9`. This connector — `TogglConnector` (`CONNECTOR_TYPE = "toggl"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from Toggl:

| Surface | Base path | Capability |
|---|---|---|
| Me | `/me` | Authenticated user profile, default workspace |
| Workspaces | `/workspaces` | List + read workspaces under the account |
| Projects | `/workspaces/{wid}/projects` | List, get, create projects |
| Time Entries | `/me/time_entries`, `/workspaces/{wid}/time_entries` | List, current, create, update, stop, delete |
| Tags | `/workspaces/{wid}/tags` | List tags |
| Clients | `/workspaces/{wid}/clients` | List + create clients |
| Tasks | `/workspaces/{wid}/projects/{pid}/tasks` | List project tasks (premium feature) |

The connector normalises projects + time entries into `NormalizedDocument` (id = `f"{connector_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429 / 5xx with exponential backoff, and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::TogglHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Reserved for higher-level retry decorators if needed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`, `pytest-timeout`.

## 3. Auth Flow

Toggl Track REST API v9 uses **HTTP Basic authentication** for server-to-server integrations.

### The "api_token" quirk

Toggl's auth scheme is unusual: the **API token is the username**, and the **literal string `"api_token"` is the password**. The `Authorization` header is:

```
Authorization: Basic base64("<api_token>:api_token")
```

This is NOT a bearer token. The `httpx` client uses `auth=(api_token, "api_token")` which produces the correct header automatically. Do NOT replace this with `Bearer <token>` — Toggl returns 403 if you do.

### Credentials

- `api_token` — Toggl Track personal API token, found under **Profile Settings → API Token** at https://track.toggl.com/profile. Stored as install_field (type `secret`, required).
- `default_workspace_id` — Workspace UUID/int used when a method omits `workspace_id`. install_field (type `text`, optional — falls back to `me.default_workspace_id`).
- `base_url` — Override of `https://api.track.toggl.com/api/v9` (rarely needed). install_field (type `text`, optional).
- `rate_limit_per_min` — Soft client-side cap. install_field (type `number`, optional, default 60).

### Lifecycle

- `install()` validates `api_token` is non-empty. Does **not** call the API (consistent with Wix template).
- `authorize()` — NOT implemented (`api_key` flow has no exchange).
- `health_check()` — `GET /me` as the canonical probe (lightest authenticated endpoint).
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Project → NormalizedDocument

| NormalizedDocument | Toggl JSON | Notes |
|---|---|---|
| `id` | `f"{connector_id}_{project['id']}"` | tenant-scoped via connector_id |
| `source_id` | `str(project["id"])` | Toggl project int id |
| `title` | `project["name"]` | |
| `content` | `project.get("name", "") + " — " + (project.get("client_name") or "")` | |
| `source` | `"toggl.project"` | |
| `created_at` | `project["created_at"]` | RFC 3339 / ISO 8601 |
| `updated_at` | `project["at"]` | Toggl uses `at` for last-modified |
| `metadata` | `{workspace_id, client_id, active, billable, color, rate, currency}` | |

### 4.2 Time Entry → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{connector_id}_{entry['id']}"` |
| `source_id` | `str(entry["id"])` |
| `title` | `entry.get("description") or f"Time entry {entry['id']}"` |
| `content` | description + duration summary |
| `source` | `"toggl.time_entry"` |
| `created_at` | `entry["start"]` |
| `updated_at` | `entry["at"]` |
| `metadata` | `{workspace_id, project_id, task_id, billable, duration, stop, tags, user_id}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/me` | Lightweight auth probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates projects + time entries per workspace | Calls `ingest_document`. |
| `get_me()` | GET | `/me` | Authenticated user profile. |
| `list_workspaces()` | GET | `/workspaces` | Workspaces the user belongs to. |
| `get_workspace(workspace_id)` | GET | `/workspaces/{wid}` | |
| `list_projects(workspace_id, *, active=None, page=None, per_page=None)` | GET | `/workspaces/{wid}/projects` | Pagination via `page` + `per_page`. |
| `get_project(workspace_id, project_id)` | GET | `/workspaces/{wid}/projects/{pid}` | |
| `create_project(workspace_id, project)` | POST | `/workspaces/{wid}/projects` | Body: project JSON. |
| `list_time_entries(*, start_date=None, end_date=None, since=None)` | GET | `/me/time_entries` | Date query params (RFC 3339). |
| `get_current_time_entry()` | GET | `/me/time_entries/current` | Returns currently-running entry or null. |
| `create_time_entry(workspace_id, entry)` | POST | `/workspaces/{wid}/time_entries` | Body: time entry JSON. |
| `update_time_entry(workspace_id, time_entry_id, entry)` | PUT | `/workspaces/{wid}/time_entries/{teid}` | |
| `stop_time_entry(workspace_id, time_entry_id)` | PATCH | `/workspaces/{wid}/time_entries/{teid}/stop` | Stops a running entry. |
| `delete_time_entry(workspace_id, time_entry_id)` | DELETE | `/workspaces/{wid}/time_entries/{teid}` | |
| `list_tags(workspace_id)` | GET | `/workspaces/{wid}/tags` | |
| `list_clients(workspace_id)` | GET | `/workspaces/{wid}/clients` | |
| `create_client(workspace_id, client)` | POST | `/workspaces/{wid}/clients` | Body: client JSON. |
| `list_tasks(workspace_id, project_id)` | GET | `/workspaces/{wid}/projects/{pid}/tasks` | Premium feature; 402 mapped to TogglError. |

Wire convention: Toggl uses **snake_case** in JSON (`workspace_id`, `default_workspace_id`, `created_at`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Toggl meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `TogglBadRequestError` (raise) |
| 401 | API token invalid / missing | `TogglAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Wrong password ("api_token" missing) / lacks perms | `TogglAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found (workspace/project/entry) | `TogglNotFoundError` (raise) |
| 409 | Conflict | `TogglConflictError` |
| 429 | Rate limited (Toggl ~1 req/sec per IP) | `TogglRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `TogglServerError` → retry with exponential backoff |

All in `exceptions.py` extending `TogglError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt` for 5xx/429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx, pytest-timeout are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_token` | secret | yes | install_field | Used as Basic auth username; "api_token" literal as password |
| `default_workspace_id` | text | no | install_field | Per-call default workspace |
| `base_url` | text | no | install_field (default `https://api.track.toggl.com/api/v9`) | Override for Toggl sandbox / proxy |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

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
| `client/http_client.py` | Single owner of httpx. Builds Basic auth header, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Toggl payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry`, ISO date parsing, safe nested get. | (stdlib only) |
| `models.py` | Pydantic schemas with snake_case fields for request bodies. | `pydantic` |
| `exceptions.py` | `TogglError` hierarchy + back-compat aliases. | (stdlib) |
| `__init__.py` | Re-export `TogglConnector`. | `connector` |

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
