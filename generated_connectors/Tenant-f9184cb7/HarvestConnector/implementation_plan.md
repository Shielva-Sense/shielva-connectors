# Harvest Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Harvest** is a time-tracking + invoicing SaaS exposing a REST API under `https://api.harvestapp.com/v2`. This connector — `HarvestConnector` (`CONNECTOR_TYPE = "harvest"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Harvest account:

| Surface | Base path | Capability |
|---|---|---|
| Time Entries | `/time_entries` | List, get, create, update, delete (the core Harvest signal) |
| Projects | `/projects` | List + read active projects |
| Tasks | `/tasks` | List task definitions |
| Clients | `/clients` | List + read clients (engagements) |
| Users | `/users`, `/users/me` | List users; identify the authenticated user |
| Invoices | `/invoices` | List invoices (drafts / open / paid / closed) |
| Estimates | `/estimates` | List estimates / quotes |
| Expenses | `/expenses` | List logged expenses |
| Reports | `/reports/time/projects` | Project hours-summary report |

The connector normalises time entries + invoices + clients into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::HarvestHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator for `HarvestRateLimitError`-style 429 handling |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Harvest API uses **Personal Access Token + Account ID** for server-to-server access.

### Credentials
- `access_token` — Personal Access Token generated at https://id.getharvest.com/developers. Stored as install_field (type `secret`, required). Sent as `Authorization: Bearer <access_token>`.
- `account_id` — Harvest Account ID (numeric, shown on the same page as the PAT). install_field (type `text`, required). Sent as `Harvest-Account-Id: <account_id>` header.
- `user_agent` — Harvest **requires** a `User-Agent` header naming the integration (per Harvest docs). install_field (type `text`, default `Shielva Harvest Connector (support@shielva.ai)`).

### Header contract
Every request to `https://api.harvestapp.com/v2/*`:

```
Authorization:       Bearer <access_token>
Harvest-Account-Id:  <account_id>
User-Agent:          <user_agent>
Content-Type:        application/json
Accept:              application/json
```

### Lifecycle
- `install()` validates `access_token` + `account_id` are non-empty. Does **not** call the API.
- `authorize()` — NOT implemented (PAT flow has no exchange). Returns a synthetic TokenInfo for ABI compatibility.
- `health_check()` — `GET /users/me` as a lightweight probe.
- `ensure_token()` — N/A (PATs do not expire).

## 4. Data Model

### 4.1 TimeEntry → NormalizedDocument

| NormalizedDocument | Harvest JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{entry['id']}"` | tenant-scoped |
| `source_id` | `str(entry["id"])` | numeric Harvest id |
| `title` | `f"Time entry {entry['id']}"` | |
| `content` | `notes` + project/task names | |
| `source` | `"harvest.time_entries"` | |
| `created_at` | `entry["created_at"]` | RFC 3339 |
| `updated_at` | `entry["updated_at"]` | |
| `metadata` | `{spent_date, hours, project, task, user, is_billed, is_locked}` | |

### 4.2 Invoice → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{inv['id']}"` |
| `source_id` | `str(inv["id"])` |
| `title` | `f"Invoice {inv['number']}"` |
| `content` | summary string (state + amount + due) |
| `source` | `"harvest.invoices"` |
| `created_at` | `inv["created_at"]` |
| `metadata` | `{state, number, amount, currency, due_date, paid_date, client.id, client.name}` |

### 4.3 Client → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{c['id']}"` |
| `source_id` | `str(c["id"])` |
| `title` | `c["name"]` |
| `content` | concat name + address + details |
| `source` | `"harvest.clients"` |
| `created_at` | `c["created_at"]` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/users/me` | Lightweight authenticated probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates time_entries + invoices + clients | Calls `ingest_document`. |
| `get_user_me()` | GET | `/users/me` | Authenticated user. |
| `list_users(*, is_active=True, page=1, per_page=100)` | GET | `/users` | Pagination. |
| `list_clients(*, is_active=True, page=1, per_page=100)` | GET | `/clients` | |
| `list_projects(*, is_active=True, client_id=None, page=1, per_page=100)` | GET | `/projects` | Optional client filter. |
| `get_project(project_id)` | GET | `/projects/{id}` | |
| `list_tasks(*, is_active=True, page=1, per_page=100)` | GET | `/tasks` | |
| `list_time_entries(*, from_date=None, to_date=None, user_id=None, project_id=None, client_id=None, is_billed=None, page=1, per_page=100)` | GET | `/time_entries` | Date-range + filters. |
| `get_time_entry(time_entry_id)` | GET | `/time_entries/{id}` | |
| `create_time_entry(project_id, task_id, spent_date, *, hours=None, notes=None, user_id=None)` | POST | `/time_entries` | |
| `update_time_entry(time_entry_id, fields)` | PATCH | `/time_entries/{id}` | |
| `delete_time_entry(time_entry_id)` | DELETE | `/time_entries/{id}` | |
| `list_invoices(*, state=None, client_id=None, page=1, per_page=100)` | GET | `/invoices` | |
| `list_estimates(*, state=None, client_id=None, page=1, per_page=100)` | GET | `/estimates` | |
| `list_expenses(*, from_date=None, to_date=None, user_id=None, project_id=None, page=1, per_page=100)` | GET | `/expenses` | |

Wire convention: Harvest uses **snake_case** JSON (`spent_date`, `is_billable`, `next_page`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Harvest meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `HarvestBadRequestError` (raise) |
| 401 | PAT invalid / missing | `HarvestAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (PAT lacks scope) | `HarvestAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `HarvestNotFoundError` |
| 422 | Unprocessable entity (validation) | `HarvestBadRequestError` |
| 429 | Rate limited (Harvest: 100 req/min; honours `Retry-After`) | `HarvestRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `HarvestServerError` → retry with exponential backoff |

All in `exceptions.py` extending `HarvestError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt`.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `access_token` | secret | yes | install_field | `Authorization: Bearer …` |
| `account_id` | text | yes | install_field | `Harvest-Account-Id` header |
| `user_agent` | text | no | install_field (default `Shielva Harvest Connector (support@shielva.ai)`) | Required by Harvest |
| `base_url` | text | no | install_field (default `https://api.harvestapp.com/v2`) | Override for sandbox / proxy |
| `rate_limit_per_min` | number | no | install_field (default 100) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["access_token", "account_id"]
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
| `helpers/normalizer.py` | Maps raw Harvest payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Date / pagination helpers, retry wrapper. | (stdlib only) |
| `models.py` | Pydantic schemas with snake_case fields for request bodies. | `pydantic` |
| `exceptions.py` | `HarvestError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `HarvestConnector`. | `connector` |

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
