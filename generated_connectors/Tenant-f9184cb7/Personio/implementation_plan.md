# Personio Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Personio** is a European HR platform (employees, attendances, time-offs, documents, custom attributes, recruitment). It exposes a v1 REST API under `https://api.personio.de/v1`. This connector — `PersonioConnector` (`CONNECTOR_TYPE = "personio"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Personio account:

| Surface | Base path | Capability |
|---|---|---|
| Auth | `/auth` | OAuth2 client-credentials → rotating bearer |
| Employees | `/company/employees` | List, get, create, update employees + custom attributes |
| Attendances | `/company/attendances` | List + create time-tracking entries |
| Time-Offs | `/company/time-offs` + `/company/time-off-types` | Book absences, list absence types |
| Documents | `/company/document-categories` + `/company/employees/{id}/documents` | List categories, upload to employee file |
| Departments / Offices | `/company/departments` + `/company/offices` | Org-structure read |
| Custom Attributes | `/company/employees/custom-attributes` | Schema introspection |
| Recruitment | `/recruiting/applications` + `/recruiting/applicants/{id}` | List ATS applications, fetch applicant details |

The connector normalises **employees** into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::PersonioHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Personio v1 uses an **OAuth2 client-credentials** grant but advertises itself in connector metadata as `api_key` because the user-facing install fields are just two pre-shared secrets (`client_id` + `client_secret`). The lifecycle is:

1. **Token mint** — `POST /auth?client_id=…&client_secret=…` with no body returns:
   - `Authorization: Bearer <jwt>` response header (canonical channel)
   - `{"success": true, "data": {"token": "<jwt>"}}` JSON body (redundant fallback)
2. **Token rotation** — **every** subsequent successful response also carries a fresh `Authorization` response header. The HTTP client transparently overwrites its cached token with that value, so the *next* request uses the rotated credential. Re-using a prior token typically returns 401.
3. **Stale-token recovery** — on 401 the client purges the cache, re-runs `/auth`, retries the original request **once**.

### Credentials
- `client_id` — Personio API Client ID. install_field (type `string`, required).
- `client_secret` — Personio API Client Secret. install_field (type `secret`, required).
- `partner_id` (optional) — `X-Personio-Partner-ID` header (default `SHIELVA`).
- `app_id` (optional) — `X-Personio-App-ID` header (default `shielva-connector`).
- `base_url` (optional) — defaults to `https://api.personio.de/v1`.

### Header contract
Every authenticated request to `https://api.personio.de/v1/*`:

```
Authorization: Bearer <jwt>            (rotated; updated from each response)
Accept:        application/json
X-Personio-Partner-ID: SHIELVA
X-Personio-App-ID:     shielva-connector
```

### Lifecycle
- `install()` validates `client_id` + `client_secret` are non-empty. Does **not** call the API.
- `authorize()` — implemented as a wrapper over `authenticate()` so the gateway's standard `authorize → set_token` lifecycle works.
- `authenticate()` — `POST /auth`, captures header + persists via `self.set_token()`.
- `health_check()` — `GET /company/employees?limit=1` as a lightweight probe.
- `ensure_token()` — first call triggers `authenticate()` under an `asyncio.Lock` to avoid stampedes.

## 4. Data Model

### 4.1 Employee → NormalizedDocument

Personio wraps every attribute in `{label, value, type, …}`. The normalizer dereferences `.value` and recurses into nested `{type, attributes:{name}}` shapes for `department` / `position`.

| NormalizedDocument | Personio JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{employee_id}"` | tenant-scoped |
| `source_id` | `str(attributes.id.value)` | Personio numeric ID |
| `title` | `"{first_name} {last_name}"` or email | |
| `content` | multi-line block: name, email, dept, position, hire date, status | |
| `source` | `"personio"` | |
| `source_url` | `https://app.personio.com/employees/{id}` | |
| `created_at` | `attributes.created_at.value` | ISO-8601 |
| `updated_at` | `attributes.last_modified_at.value` | |
| `metadata` | `{first_name, last_name, email, department, position, hire_date, status}` | |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `authenticate()` | POST | `/auth` | Mint + cache rotating bearer. |
| `health_check()` | GET | `/company/employees?limit=1` | Probe employees endpoint. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates `/company/employees` | Page via `limit`+`offset`. |
| `list_employees(*, limit, offset, email, updated_since)` | GET | `/company/employees` | Filter by email or updated_since. |
| `get_employee(employee_id)` | GET | `/company/employees/{id}` | |
| `create_employee(...)` | POST | `/company/employees` | Body `{employee: {attributes: {…}}}`. |
| `update_employee(employee_id, attributes)` | PATCH | `/company/employees/{id}` | Body `{employee: {attributes: {…}}}`. |
| `list_attendances(*, employees, start_date, end_date, limit, offset)` | GET | `/company/attendances` | Array param `employees[]`. |
| `create_attendance(employee, date, start_time, end_time, break_time, comment)` | POST | `/company/attendances` | Body `{attendances: [{…}]}`. |
| `update_attendance(attendance_id, attributes)` | PATCH | `/company/attendances/{id}` | |
| `list_time_offs(*, start_date, end_date, limit, offset)` | GET | `/company/time-offs` | |
| `create_time_off(employee_id, time_off_type_id, start_date, end_date, half_day_start, half_day_end)` | POST | `/company/time-offs` | |
| `list_time_off_types()` | GET | `/company/time-off-types` | |
| `list_documents(employee_id, category_id=None)` | GET | `/company/document-categories` | |
| `upload_document(employee_id, file_bytes, filename, category_id, title)` | POST | `/company/employees/{id}/documents` | multipart upload. |
| `list_custom_attributes()` | GET | `/company/employees/custom-attributes` | |
| `list_departments()` | GET | `/company/departments` | |
| `list_offices()` | GET | `/company/offices` | |
| `list_projects()` | GET | `/company/projects` | |
| `list_applications(*, limit, offset, status)` | GET | `/recruiting/applications` | |
| `get_applicant(applicant_id)` | GET | `/recruiting/applicants/{id}` | |

Wire convention: Personio uses **snake_case** in JSON (`first_name`, `hire_date`, `time_off_type_id`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Personio meaning | Mapped to |
|---|---|---|
| 400 | Bad request (validation, missing required field) | `PersonioBadRequestError` |
| 401 | Invalid / stale token | `PersonioAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED`; one auto re-auth retry |
| 403 | Scope/permission missing on the API key | `PersonioAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Resource not found | `PersonioNotFoundError` |
| 409 | Conflict (e.g. duplicate employee email) | `PersonioConflictError` |
| 429 | Rate limited (Personio sends `Retry-After`) | `PersonioRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `PersonioServerError` → exponential backoff retry |
| transport | timeouts, connection resets | `PersonioNetworkError` → retry |

All in `exceptions.py` extending `PersonioError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `min(2 ** attempt, 32)` for 5xx/network, honors `Retry-After` for 429.

### Token rotation gotcha (READ THIS)

Personio rotates the bearer on **every successful response**. The client MUST:

1. Read the response `Authorization` header before parsing the body.
2. Overwrite `self._token` with the rotated value.
3. Persist via `self.set_token()` so a restart picks up the latest credential.
4. Never include the rotated token in log output.

Failure to rotate manifests as 401 on the *next* call (not the current one). The auto re-auth path covers this, but every redundant 401 burns a quota slot — keep rotation correct.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
# httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.
```

There are no Personio-specific extras — the connector uses only pre-installed packages.

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `client_id` | string | yes | install_field | OAuth2 client_id |
| `client_secret` | secret | yes | install_field | OAuth2 client_secret |
| `partner_id` | string | no | install_field (default `SHIELVA`) | `X-Personio-Partner-ID` header |
| `app_id` | string | no | install_field (default `shielva-connector`) | `X-Personio-App-ID` header |
| `base_url` | string | no | install_field (default `https://api.personio.de/v1`) | Override for sandbox |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["client_id", "client_secret"]
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
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, captures rotated token, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Personio payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry harness around `PersonioNetworkError`; chunked iterator. | `asyncio`, `random`, `structlog`, `exceptions` |
| `models.py` | Local dataclasses (`PersonioEmployee`, `PersonioAbsence`, `PersonioAttendance`, …) for callers that prefer typed access. | `dataclasses`, `shared.base_connector` |
| `exceptions.py` | `PersonioError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `PersonioConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is a standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` — no hardcoded tenant data ✓
9. Features (retry, rotation, pagination) as composable helpers ✓
10. Error mapping in `exceptions.py`; `connector.py` catches custom exceptions only ✓

**Score: 10/10.**
