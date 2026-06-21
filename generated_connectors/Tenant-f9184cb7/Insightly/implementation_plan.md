# Insightly Connector — Implementation Plan

## 1. Overview

**Insightly** is an SMB-focused CRM with a comprehensive REST API (`v3.1`) covering
contacts, organisations, opportunities, projects, tasks, leads, events, notes,
emails, pipelines, custom objects, users, and tags. This connector —
`InsightlyConnector` (`CONNECTOR_TYPE = "insightly"`, `AUTH_TYPE = "api_key"`) —
wraps the operational surfaces a Shielva tenant typically needs:

| Surface | Endpoint root | Capabilities |
|---|---|---|
| Contacts | `/Contacts` | list, get, create, update, delete |
| Organisations | `/Organisations` | list, get, create, update, delete |
| Opportunities | `/Opportunities` | list, get, create, update, delete |
| Leads | `/Leads` | list, get, create, update, delete |
| Projects | `/Projects` | list, get, create, update, delete |
| Tasks | `/Tasks` | list, get, create, update, delete |
| Events | `/Events` | list, get |
| Notes | `/Notes` | list, get |
| Emails | `/Emails` | list, get |
| Pipelines | `/Pipelines` | list |
| Users | `/Users` | list (health-check probe via `/Users/Me`) |
| Custom Objects | `/CustomObjects` | list |
| Tags | `/Tags` | list |

### Pod-aware base URL convention

Insightly hosts each tenant on a **regional pod** (`na1`, `eu1`, `apac1`, …).
The REST base URL is therefore:

```
https://api.{pod}.insightly.com/v3.1
```

The connector accepts `pod` as a required install field (default `"na1"`) and
constructs the base URL at `__init__` time. Operators on a custom/private
deployment may override the whole base URL with the optional `base_url`
install field (rare).

### Capabilities

- Per-tenant config (`api_key`, `pod`) — no hardcoded creds.
- HTTP Basic auth (`api_key` as username, empty password).
- Pod-aware base URL (`na1` default; override via `pod` or `base_url`).
- 429 / 5xx exponential backoff at the HTTP layer (3 attempts).
- Typed exception hierarchy (`InsightlyAuthError`, `InsightlyNotFound`, …).
- `_STATUS_MAP` for health-check classification (401/403/429).
- `health_check()` probes `/Users/Me` (cheapest authenticated call — single
  user record).
- `sync()` pages through Contacts → `NormalizedDocument` and ingests via
  `ingest_document()`.
- Multi-tenant safe: NormalizedDocument id format is
  `f"{tenant_id}_{source_id}"`.

## 2. SDK / Package Selection

| Package | Version | Why |
|---|---|---|
| `httpx` | `>=0.27.0` | Async HTTP client, native HTTP/2, used across all Shielva connectors. |
| `structlog` | `>=24.0.0` | Structured logging matching the rest of the platform. |
| `pydantic` | `>=2.6.0` | Request/response models (`models.py`). |

No Insightly SDK exists; we call the REST API directly with `httpx`.

Dev/test:

| Package | Version | Why |
|---|---|---|
| `pytest` | `>=8.0.0` | Test runner. |
| `pytest-asyncio` | `>=0.23.0` | `asyncio_mode = auto`. |
| `pytest-mock` | `>=3.12.0` | `mocker` fixture for autouse storage/logger mocks. |
| `respx` | `>=0.21.0` | httpx route mocking — zero real I/O. |

## 3. Auth Flow

Insightly uses **HTTP Basic** with the API key as the username and an empty
password:

```
Authorization: Basic base64(api_key + ":")
```

There is no OAuth handshake, no refresh, no expiry. The connector flow:

| Phase | Behaviour |
|---|---|
| `install()` | Validates `api_key` and `pod` are non-empty. Persists the config via `save_config()`. Does **not** call the API. |
| `authorize(auth_code, state)` | No-op — returns a `TokenInfo` whose `access_token` is the API key (surface compatibility with the BaseConnector ABI). |
| `health_check()` | `GET /Users/Me` — minimal authenticated probe. 200 → HEALTHY+CONNECTED. 401 → DEGRADED+TOKEN_EXPIRED. 403 → UNHEALTHY+INVALID_CREDENTIALS. 429 → DEGRADED+CONNECTED. |
| `ensure_token()` | Never called — no token to refresh. |
| token storage | None. The API key lives in `self.config` (encrypted by the gateway). |

## 4. Data Model

The sync target is **Insightly Contacts**. Each Contact maps to a
`NormalizedDocument`:

| `NormalizedDocument` field | Insightly Contact source |
|---|---|
| `id` | `f"{tenant_id}_{CONTACT_ID}"` |
| `source_id` | `str(CONTACT_ID)` |
| `title` | `"{FIRST_NAME} {LAST_NAME}".strip()` or `"Contact {id}"` |
| `content` | Text block: name + email + phone + BACKGROUND |
| `content_type` | `"text"` |
| `source` | `"insightly"` |
| `author` | First email in `EMAILADDRESSES` |
| `created_at` | `DATE_CREATED_UTC` |
| `updated_at` | `DATE_UPDATED_UTC` |
| `metadata` | `{kind: "insightly.contact", organisation_id, raw: <original>}` |
| `tenant_id` | from connector |
| `connector_id` | from connector |

Equivalent helpers in `helpers/normalizer.py` exist for Organisations, Opportunities, Leads.

## 5. Key API Endpoints & Methods

Every method here is a standalone `async def` on `InsightlyConnector`
(OCP — never folded into `sync()`).

### `async install() → ConnectorStatus`
Validate `api_key` + `pod`, persist config. No outbound call.

### `async authorize(auth_code: str = "", state: str = "") → TokenInfo`
No-op for api_key. Returns `TokenInfo(access_token=api_key, token_type="api_key")`.

### `async health_check() → ConnectorStatus`
`GET /Users/Me`. Maps 401/403/429 via `_STATUS_MAP`.

### `async sync(since=None, full=False, kb_id=None, webhook_url=None) → SyncResult`
Pages through `/Contacts?top=200&skip=<offset>`, normalizes each Contact via
`normalize_contact`, and calls `ingest_document`. Stores the last skip offset
under metadata key `"last_skip"`; full=True resets to 0.

### Contacts
- `list_contacts(top=50, skip=0, brief=False) → list[dict]` → `GET /Contacts?top=&skip=&brief=`
- `get_contact(contact_id: int) → dict` → `GET /Contacts/{id}`
- `create_contact(first_name?, last_name?, email?, phone?) → dict` → `POST /Contacts`
- `update_contact(contact_id, fields: dict) → dict` → `PUT /Contacts/{id}` (Insightly PUT requires CONTACT_ID in body)
- `delete_contact(contact_id) → dict` → `DELETE /Contacts/{id}` (idempotent — 404 → `already_missing=True`)

### Organisations
- `list_organisations(top=50, skip=0)` → `GET /Organisations`
- `get_organisation(organisation_id)` → `GET /Organisations/{id}`
- `create_organisation(organisation_name, phone?, website?) → dict` → `POST /Organisations`
- `update_organisation(organisation_id, fields)` → `PUT /Organisations/{id}`
- `delete_organisation(organisation_id)` → `DELETE /Organisations/{id}`

### Opportunities
- `list_opportunities(top=50, skip=0)` → `GET /Opportunities`
- `get_opportunity(opportunity_id)` → `GET /Opportunities/{id}`
- `create_opportunity(opportunity_name, opportunity_value=0.0, probability=50, bid_currency="USD")` → `POST /Opportunities`
- `update_opportunity(opportunity_id, fields)` → `PUT /Opportunities/{id}`
- `delete_opportunity(opportunity_id)` → `DELETE /Opportunities/{id}`

### Leads
- `list_leads(top=50, skip=0)` → `GET /Leads`
- `get_lead(lead_id)` → `GET /Leads/{id}`
- `create_lead(first_name?, last_name?, email?, lead_source_id?)` → `POST /Leads`
- `update_lead(lead_id, fields)` → `PUT /Leads/{id}`
- `delete_lead(lead_id)` → `DELETE /Leads/{id}`

### Projects
- `list_projects(top=50, skip=0)` → `GET /Projects`
- `get_project(project_id)` → `GET /Projects/{id}`
- `create_project(project_name, status="In Progress")` → `POST /Projects`
- `update_project(project_id, fields)` → `PUT /Projects/{id}`
- `delete_project(project_id)` → `DELETE /Projects/{id}`

### Tasks
- `list_tasks(top=50, skip=0)` → `GET /Tasks`
- `get_task(task_id)` → `GET /Tasks/{id}`
- `create_task(title, status="Not Started", priority=2)` → `POST /Tasks`
- `update_task(task_id, fields)` → `PUT /Tasks/{id}`
- `delete_task(task_id)` → `DELETE /Tasks/{id}`

### Read-only surfaces
- `list_events(top=50, skip=0)` → `GET /Events`
- `list_notes(top=50, skip=0)` → `GET /Notes`
- `list_emails(top=50, skip=0)` → `GET /Emails`
- `list_pipelines()` → `GET /Pipelines`
- `list_users()` → `GET /Users`
- `list_custom_objects()` → `GET /CustomObjects`
- `list_tags(record_type="contacts")` → `GET /Tags/{record_type}`

Pagination is OData-style: `?top=N&skip=M`. Each list returns a JSON array
directly (no envelope).

## 6. Error Handling

`exceptions.py`:

```
InsightlyError                        # base — status_code + response_body
├── InsightlyAuthError                # 401 / 403
├── InsightlyBadRequestError          # 400
├── InsightlyNotFoundError (alias: InsightlyNotFound)   # 404
├── InsightlyConflictError            # 409
├── InsightlyRateLimitError           # 429 — retry_after_s
└── InsightlyServerError (alias: InsightlyNetworkError) # 5xx, transport
```

Retry table (`client/http_client.py::_request`):

| Status | Action |
|---|---|
| 400 / 401 / 403 / 404 / 409 | Raise immediately |
| 429 | Exponential backoff `_BACKOFF_BASE * 2 ** attempt` (0.5s, 1s, 2s), 3 attempts |
| 5xx | Same backoff, 3 attempts |
| `httpx.TimeoutException` / `httpx.NetworkError` | Backoff, 3 attempts, then `InsightlyNetworkError` |

`health_check()` classifies via `_STATUS_MAP`:

```
401 → ConnectorStatus(DEGRADED,  TOKEN_EXPIRED)
403 → ConnectorStatus(UNHEALTHY, INVALID_CREDENTIALS)
429 → ConnectorStatus(DEGRADED,  CONNECTED)
```

## 7. Dependencies

```
pip install httpx>=0.27.0 structlog>=24.0.0 pydantic>=2.6.0
# dev
pip install pytest>=8.0.0 pytest-asyncio>=0.23.0 pytest-mock>=3.12.0 respx>=0.21.0
```

`requirements.txt` lists only the runtime deps; test deps live in `requirements-dev.txt`
(or in the orchestrator's test image).

## 8. Config & Install Fields

### User-provided install fields

| Key | Type | Required | Read in code | Purpose |
|---|---|---|---|---|
| `api_key` | secret | yes | `self.api_key` | HTTP Basic username; password is empty |
| `pod` | string | yes (default `"na1"`) | `self.pod` | Region pod — controls base URL |
| `base_url` | string | no | `self.base_url` (computed from pod) | Optional full override |
| `rate_limit_per_min` | number | no (default `60`) | `self.rate_limit_per_min` | Soft cap |

### Class constants

```python
REQUIRED_CONFIG_KEYS = ["api_key", "pod"]
_STATUS_MAP = {
    401: ("DEGRADED",  "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
_INSIGHTLY_BASE_TEMPLATE = "https://api.{pod}.insightly.com/v3.1"
```

`api_key` is **never** a class constant — it's per-tenant and read from
`self.config.get("api_key")` only.

## 9. SOC / OCP Architecture Plan

| File | Responsibility | Forbidden |
|---|---|---|
| `connector.py` | Orchestrate — `install`, `authorize`, `health_check`, `sync`, per-surface async methods. Calls `self.http_client.*` only. | Raw `httpx` calls, JSON parsing, normalization, retry math. |
| `client/http_client.py` | All HTTP — `_request`, per-endpoint methods, retry on 429/5xx, status-code → typed-exception mapping, basic-auth header. | Business logic, normalization, BaseConnector imports. |
| `helpers/normalizer.py` | Raw Insightly JSON → `NormalizedDocument`. `normalize_contact`, `normalize_organisation`, `normalize_opportunity`, `normalize_lead`. | HTTP, retry, secrets. |
| `helpers/utils.py` | `with_retry` (escape hatch for flaky transports), `safe_get` (nested-dict walker), `build_basic_auth_header`. | HTTP, normalization. |
| `exceptions.py` | Typed exception hierarchy. | Anything else. |
| `models.py` | Pydantic request/response shapes (`Contact`, `Opportunity`, `QueryRequest`, …). | Side effects. |
| `metadata/connector.json` | Gateway-readable connector descriptor — `apis`, `install_fields`, `dependencies`. | Code. |
| `.shielva/docs/connector_docs.json` | In-app docs (7 sections). | Code. |

SOC checks (must score 10/10):
1. `connector.py` has zero raw `httpx` calls.
2. All HTTP in `client/http_client.py`.
3. All normalization in `helpers/normalizer.py`.
4. All shared helpers in `helpers/utils.py`.
5. `connector.py` imports from `client/` and `helpers/` only.

OCP checks:
6. Every user-requested operation is a standalone `async def`.
7. Adding a new surface (e.g. `/Comments`) requires no edit to existing methods.
8. All credentials/URLs come from `self.config.get`.
9. Retry/pagination are composable helpers.
10. `connector.py` catches typed exceptions only (`InsightlyAuthError`, …).
