# Hubstaff Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Hubstaff** is a time-tracking + workforce-analytics platform exposing a REST API suite under `https://api.hubstaff.com/v2`. This connector — `HubstaffConnector` (`CONNECTOR_TYPE = "hubstaff"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Hubstaff workspace:

| Surface | Base path | Capability |
|---|---|---|
| Organizations | `/organizations` | List org workspaces the token belongs to |
| Users | `/users` | List + read workspace users; `/users/me` for the caller |
| Teams | `/teams` | List teams inside an organization |
| Projects | `/projects` | List + read + create projects |
| Tasks | `/tasks` | List tasks scoped by project / org |
| Activities | `/activities` | Tracked-time activity rows (per-user, per-project, time-slot) |
| Daily Activities | `/activities/daily` | Per-user/day rolled-up summaries |
| Time Entries | `/time_entries` | Manual + tracked time entries between dates |
| Screenshots | `/screenshots` | Screenshot metadata (URL, time, user) |
| Apps | `/application_activities` | Application usage rows |
| URLs | `/url_activities` | URL/website usage rows |
| Notes | `/notes` | Notes attached to time entries |

The connector normalises projects + tasks + daily activities into `NormalizedDocument` (id = `f"{connector_id}_{source_id}"` per the shared base), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::HubstaffHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Hubstaff REST API uses **bearer-token authentication** with a long-lived Personal Access Token (PAT). The connector treats the PAT as an opaque API key (`AUTH_TYPE = "api_key"`) — there is no OAuth dance, no refresh exchange, and no token rotation.

### Credentials
- `access_token` — Hubstaff Personal Access Token created in **Hubstaff Settings → Developer → Personal Access Tokens**. Stored as install_field (type `secret`, required).
- `default_organization_id` — Numeric organization ID used as a default in `sync()` and convenience calls. install_field (type `number`, optional).

### Header contract
Every request to `https://api.hubstaff.com/v2/*`:

```
Authorization: Bearer <access_token>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `access_token` is non-empty. Does **not** call the API.
- `authorize()` — NOT implemented (`api_key` flow has no exchange); returns a synthetic `TokenInfo` whose `access_token` is the PAT.
- `health_check()` — `GET /users/me` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Project → NormalizedDocument

| NormalizedDocument | Hubstaff JSON | Notes |
|---|---|---|
| `id` | `f"{connector_id}_{project['id']}"` | connector-scoped |
| `source_id` | `str(project["id"])` | Hubstaff project ID |
| `title` | `project["name"]` | |
| `content` | `project["description"]` (fallback to name) | |
| `created_at` | `project["created_at"]` | RFC 3339 |
| `updated_at` | `project["updated_at"]` | |
| `metadata` | `{status, organization_id, billable, kind: "hubstaff.project"}` | |

### 4.2 Task → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{connector_id}_{task['id']}"` |
| `source_id` | `str(task["id"])` |
| `title` | `task["summary"]` |
| `content` | `task["summary"]` (Hubstaff tasks are summary-only) |
| `author` | `str(task["assignee_id"])` |
| `created_at` | `task["created_at"]` |
| `metadata` | `{project_id, status, due_at, kind: "hubstaff.task"}` |

### 4.3 Daily Activity → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{connector_id}_{activity['id']}"` |
| `source_id` | `str(activity["id"])` |
| `title` | `f"Activity {id} — user {user_id} project {project_id}"` |
| `content` | `f"tracked={tracked} idle={idle} keyboard={keyboard} mouse={mouse}"` |
| `created_at` | `activity["starts_at"]` or `activity["date"]` |
| `metadata` | `{user_id, project_id, task_id, tracked, kind: "hubstaff.daily_activity"}` |

## 5. Key API Endpoints & Methods

Every method listed below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/users/me` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates projects + tasks + daily activities | Calls `ingest_document`. |
| `list_organizations(page_start_id, page_limit)` | GET | `/organizations` | Cursor pagination. |
| `list_users(organization_id, page_start_id, page_limit)` | GET | `/organizations/{id}/members` | |
| `get_user(user_id)` | GET | `/users/{id}` | |
| `list_teams(organization_id)` | GET | `/organizations/{id}/teams` | |
| `list_projects(organization_id, status, page_start_id, page_limit)` | GET | `/organizations/{id}/projects` | |
| `get_project(project_id)` | GET | `/projects/{id}` | |
| `create_project(organization_id, name, description)` | POST | `/organizations/{id}/projects` | Body: `{project: {...}}`. |
| `list_tasks(project_id, status, page_start_id, page_limit)` | GET | `/projects/{id}/tasks` | |
| `list_activities(organization_id, date_start, date_stop, user_ids, project_ids, page_start_id, page_limit)` | GET | `/organizations/{id}/activities` | |
| `list_time_entries(organization_id, date_start, date_stop, user_ids, project_ids, page_start_id, page_limit)` | GET | `/organizations/{id}/time_entries` | |
| `list_daily_activities(organization_id, date_start, date_stop, user_ids, project_ids, page_start_id, page_limit)` | GET | `/organizations/{id}/activities/daily` | |
| `list_screenshots(organization_id, date_start, date_stop, user_ids, project_ids, page_limit)` | GET | `/organizations/{id}/screenshots` | |
| `list_apps(organization_id, date_start, date_stop, user_ids, page_limit)` | GET | `/organizations/{id}/application_activities` | |
| `list_urls(organization_id, date_start, date_stop, user_ids, page_limit)` | GET | `/organizations/{id}/url_activities` | |
| `list_notes(organization_id, date_start, date_stop, user_ids, page_limit)` | GET | `/organizations/{id}/notes` | |

Wire convention: Hubstaff uses **snake_case** in JSON (`organization_id`, `project_id`, `created_at`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Hubstaff meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `HubstaffBadRequestError` (raise) |
| 401 | Token invalid / missing header | `HubstaffAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (token lacks scope) | `HubstaffAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `HubstaffNotFoundError` (raise) |
| 409 | Conflict | `HubstaffConflictError` |
| 429 | Rate limited (`Retry-After` header honoured) | `HubstaffRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `HubstaffServerError` → retry with exponential backoff |

All in `exceptions.py` extending `HubstaffError`. Retry in `client/http_client.py::_request` honours `_MAX_RETRIES=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt` for 5xx, honouring `Retry-After` for 429.

## 7. Dependencies

Packages to install in the connector's venv (`install_deps` reads `requirements.txt`):

```
httpx>=0.27.0
```

(structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `access_token` | secret | yes | install_field | `Authorization: Bearer <...>` header value |
| `default_organization_id` | number | no | install_field | Default org for `sync()` + convenience calls |
| `base_url` | text | no | install_field (default `https://api.hubstaff.com/v2`) | Override for sandbox / proxy |
| `rate_limit_per_min` | number | no | install_field (default 60) | Per-request httpx timeout |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["access_token"]
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
| `helpers/normalizer.py` | Maps raw Hubstaff payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry helpers, ISO date parsing, list coercion. | (stdlib only) |
| `models.py` | Pydantic schemas for request bodies. | `pydantic` |
| `exceptions.py` | `HubstaffError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `HubstaffConnector`. | `connector` |

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
