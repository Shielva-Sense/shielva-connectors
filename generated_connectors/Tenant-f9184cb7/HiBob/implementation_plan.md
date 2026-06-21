# HiBob Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**HiBob** (a.k.a. **Bob**) is a modern HR / HCM platform exposing a REST API suite under `https://api.hibob.com/v1`. This connector — `HiBobConnector` (`CONNECTOR_TYPE = "hibob"`, `AUTH_TYPE = "api_key"`) — wraps the operational HR surfaces a Shielva tenant typically needs from a Bob tenant:

| Surface | Base path | Capability |
|---|---|---|
| People (employees) | `/people`, `/people/search`, `/people/{id}` | List, search, read, create, update employees |
| Profile | `/profiles/{id}` | Read the fully-projected employee profile (humanised fields) |
| Employments | `/people/{id}/employment` | Employment history per employee |
| Time-Off | `/timeoff/requests`, `/timeoff/requests/changes` | List + create time-off requests |
| Payroll | `/payroll/history` | List payroll history records |
| Reports | `/company/reports`, `/company/reports/{id}/download` | Saved reports + download |
| Lifecycle | `/people/lifecycle/changes` | List lifecycle status changes (hire, leave, return, terminate) |
| Departments | `/company/named-lists/department` | Department named-list |
| Sites | `/company/named-lists/site` | Site (office location) named-list |

The connector normalises People records into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces every user-requested operation as a standalone public `async def` on `HiBobConnector` (OCP), and routes 429/5xx through a centralised retry policy in `client/http_client.py` (SOC — connector.py never calls `httpx` directly).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

The connector deliberately avoids the HiBob "py-bob" community SDK so it matches the Bandwidth/Wix gold-standard pattern: single httpx owner in `client/http_client.py`, retry policy + auth header centralised, zero hidden global state.

## 3. Auth Flow

HiBob uses **HTTP Basic** authentication for Service Users.

### Credentials
- `service_user_id` — Service-user identifier (`SERVICE-XXXXX`) issued at **Bob → Settings → Integrations → Service Users → New**. install_field (type `string`, required).
- `service_user_token` — Service-user secret token shown once at creation. install_field (type `secret`, required).

### Header contract
Every request to `https://api.hibob.com/v1/*`:

```
Authorization: Basic base64(service_user_id:service_user_token)
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `service_user_id` + `service_user_token` are non-empty. Does **not** call the API.
- `authorize()` — NOT a real OAuth exchange; returns a synthetic `TokenInfo(token_type="Basic")` so the platform's storage contract holds.
- `health_check()` — `GET /people?limit=1` probes a single employee record; cheapest authenticated endpoint.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Employee → NormalizedDocument

| NormalizedDocument | HiBob JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{employee_id}"` | tenant-scoped per multi-tenant rule |
| `source_id` | `employee["id"]` | HiBob employee UUID |
| `title` | `employee["displayName"]` or `firstName + surname` | |
| `content` | concat name + email + title + department + site | |
| `source` | `"hibob.people"` | |
| `created_at` | `employee["startDate"]` | RFC 3339 |
| `updated_at` | `employee["modificationDate"]` | |
| `metadata` | `{email, work_email, department, site, title, manager, employeeNumber, ...}` | |

### 4.2 Time-Off Request → metadata-only

Time-off requests, payroll history, and lifecycle changes are passed through as raw dicts in the public API surface (no NormalizedDocument projection by default — the call site decides whether to ingest them).

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/people?limit=1` | Lightweight probe (1 employee, only `id` projection). |
| `sync(since, full, kb_id)` | (lifecycle) | iterates people via `/people/search` | Calls `ingest_document` per employee. |
| `list_people(*, limit=50, include_humanized=False, fields=None)` | GET | `/people?limit=N` | Simple listing — array of employee dicts. |
| `search_people(filters=None, fields=None)` | POST | `/people/search` | Body: `{filters: [...], fields: [...]}`. |
| `get_employee(employee_id)` | GET | `/people/{id}` | Single employee. |
| `get_employee_profile(employee_id)` | GET | `/profiles/{id}` | Humanised profile projection. |
| `create_employee(first_name, surname, email, ...)` | POST | `/people` | Body: `{firstName, surname, email, work: {...}}`. |
| `update_employee(employee_id, fields)` | PUT | `/people/{id}` | Body: arbitrary `fields` dict. |
| `list_employments(employee_id)` | GET | `/people/{id}/employment` | Employment history. |
| `list_time_off_requests(*, from_date, to_date, policy_type_display_name, include_pending)` | GET | `/timeoff/requests/changes` | Query params. |
| `create_time_off_request(employee_id, policy_type_display_name, request_range_type, start_date, end_date?, description?)` | POST | `/timeoff/employees/{id}/requests` | Body. |
| `list_payroll(employee_id)` | GET | `/payroll/history/{employee_id}` | Per-employee payroll history. |
| `list_lifecycle_changes(*, from_date=None, to_date=None)` | GET | `/people/lifecycle/changes` | Cursor-pageable lifecycle event log. |
| `list_departments()` | GET | `/company/named-lists/department` | Department list. |
| `list_sites()` | GET | `/company/named-lists/site` | Sites (locations) list. |

Wire convention: HiBob uses **camelCase** in JSON (`firstName`, `policyTypeDisplayName`, `startDate`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | HiBob meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `HiBobBadRequestError` (raise) |
| 401 | Service-user invalid / disabled | `HiBobAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (missing role / permission) | `HiBobAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `HiBobNotFoundError` (raise) |
| 409 | Conflict (duplicate email) | `HiBobConflictError` |
| 429 | Rate limited (honours `Retry-After` when present) | `HiBobRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Bob outage | `HiBobServerError` → retry with exponential backoff |

All in `exceptions.py` extending `HiBobError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff (0.5s, 1s, 2s + jitter, capped at 16s).

Legacy alias `HiBobNetworkError = HiBobServerError`, `HiBobNotFound = HiBobNotFoundError` preserved for older callers.

## 7. Dependencies

Connector-specific packages required at runtime (read by `install_deps`):

```
# (none beyond the pre-installed httpx/pydantic/structlog/pytest stack)
```

`requirements.txt` is therefore intentionally empty of new packages — everything we need is in the shared venv.

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `service_user_id` | string | yes | install_field | Sent as Basic-Auth username |
| `service_user_token` | secret | yes | install_field | Sent as Basic-Auth password |
| `base_url` | string | no | install_field (default `https://api.hibob.com/v1`) | Override for sandbox / proxy |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:

```python
REQUIRED_CONFIG_KEYS = ["service_user_id", "service_user_token"]
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
| `client/http_client.py` | Single owner of httpx. Builds Basic-Auth header, retries 429/5xx (with `Retry-After`), raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw HiBob employee payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry helper (`with_retry`), humanizer for nested employee fields. | (stdlib only) |
| `models.py` | Pydantic schemas with camelCase aliases for request bodies (`CreateEmployeeBody`, `TimeOffRequestBody`). | `pydantic` |
| `exceptions.py` | `HiBobError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `HiBobConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` — no hardcoded creds ✓
9. Features (retry, humanizer) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
