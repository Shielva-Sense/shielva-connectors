# SugarCRM Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**SugarCRM** is a CRM platform exposing a REST API (current major version v11; instances pin a sub-revision such as `v11_18` on SugarCloud). This connector — `SugarCRMConnector` (`CONNECTOR_TYPE = "sugarcrm"`, `AUTH_TYPE = "oauth2_password"`) — wraps the operational CRM surfaces a Shielva tenant typically needs:

| Surface | Module | Capability |
|---|---|---|
| Contacts | `/Contacts` | List · Get · Create · Update · Delete |
| Accounts | `/Accounts` | List (filtered) |
| Opportunities | `/Opportunities` | List · Create |
| Leads | `/Leads` | List · Convert |
| Meetings | `/Meetings` | List |
| Identity | `/me` | Health-check probe (lightweight) |

The connector normalises CRM records (Contacts, Accounts, Opportunities, Leads, Cases) into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and recovers from access-token expiry via a single-shot 401 → refresh → retry wrapper (`helpers.utils.refresh_and_retry_on_401`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per `CONNECTOR_SYSTEM_PROMPT` |
| `respx` | `>=0.21` | Test-only — respx mocks httpx without monkeypatching the connector |
| `pytest` / `pytest-asyncio` / `pytest-mock` | latest | Test framework |

No vendor Python SDK is used: SugarCRM ships an official Python client but it pins old `requests`/`urllib3` and is not async; the REST API is small and stable enough that a thin `httpx`-based client (`client/http_client.py`) is the cleaner long-term shape.

## 3. Auth Flow

SugarCRM v11 uses OAuth2 with two grants in scope:

| Grant | When | Mechanics |
|---|---|---|
| `password` (default) | On-prem + SugarCloud service accounts | `POST {site_url}/rest/v11/oauth2/token` with `grant_type=password`, `client_id` (default `"sugar"` — Sugar's built-in trusted client), `username`, `password`, `platform`. Returns `access_token` + `refresh_token` + `expires_in` (typically 3600). |
| `authorization_code` | SugarCloud OAuth apps | User redirected to `{site_url}/?module=OAuth2&action=authorize` with `response_type=code`. On callback, `POST {token_url}` with `grant_type=authorization_code`, `code`, `redirect_uri`. |

Both grants return the same JSON envelope:

```json
{
  "access_token":  "...",
  "refresh_token": "...",
  "expires_in":    3600,
  "token_type":    "bearer"
}
```

### Header contract

Every authenticated request:

```
OAuth-Token:  <access_token>     ← NOT 'Authorization: Bearer'
Content-Type: application/json
Accept:       application/json
```

### Refresh

`on_token_refresh()`:

1. If a `refresh_token` is on file → `POST /oauth2/token` with `grant_type=refresh_token`.
2. If refresh fails (401) AND the connector was installed via the password grant AND `username` + `password` are still in config → re-issue the password grant transparently so the gateway never sees a `RefreshError` mid-flow.
3. Otherwise raise `RefreshError`.

A single-shot 401 → refresh → retry wrapper (`helpers.utils.refresh_and_retry_on_401`) sits around every authenticated call; the first 401 triggers refresh + 1 retry, the second 401 in the same chain propagates.

## 4. Data Model

### `NormalizedDocument` shape (one per CRM record)

`helpers.normalizer.normalize_record(module, raw, tenant_id, connector_id)` dispatches to a per-module normaliser:

| Module | Title | Content | Metadata `kind` |
|---|---|---|---|
| Contacts | `first_name + last_name` | name · title · department · account · email · phone · description | `sugarcrm.contact` |
| Accounts | `name` | name · industry · website · description | `sugarcrm.account` |
| Opportunities | `name` | name · sales_stage · amount · description | `sugarcrm.opportunity` |
| Leads | `first_name + last_name` | name · company · status · lead_source · email | `sugarcrm.lead` |
| Cases | `name` | name · status · priority · description · resolution | `sugarcrm.case` |

All docs carry:
- `id = f"{tenant_id}_{source_id}"` (multi-tenant isolation)
- `created_at` ← `date_entered`
- `updated_at` ← `date_modified`
- `metadata.assigned_user_id` + `metadata.assigned_user_name`
- `tags = ["sugarcrm", <module-lower>]`

### Internal models (in `models.py`)

| Class | Purpose |
|---|---|
| `InstallResult` | `install()` return — `success`, `message`, `auth_status` / `health` shims |
| `HealthCheckResult` | `health_check()` return — same shape |
| `SyncResult` | sync summary (`documents_found / synced / failed`) |
| `ConnectorDocument` | dataclass mirror of `NormalizedDocument` for in-memory use |

## 5. Key API Endpoints & Methods

| HTTP | Path | Connector method | Notes |
|---|---|---|---|
| POST | `/oauth2/token` | `_oauth_token` (private) | password / refresh_token / auth_code |
| GET | `/me` | `health_check` | lightweight probe |
| GET | `/Contacts` | `list_contacts(offset, max_num, filter)` | offset/max_num pagination |
| GET | `/Contacts/{id}` | `get_contact(contact_id)` | |
| POST | `/Contacts` | `create_contact(first_name, last_name, email?, phone_work?)` | email rendered as `email` array w/ `primary_address=true` |
| PUT | `/Contacts/{id}` | `update_contact(contact_id, fields)` | arbitrary field bag |
| DELETE | `/Contacts/{id}` | `delete_contact(contact_id)` | |
| GET | `/Accounts` | `list_accounts(offset, max_num)` | |
| GET | `/Opportunities` | `list_opportunities(offset, max_num)` | |
| POST | `/Opportunities` | `create_opportunity(name, account_id?, amount, sales_stage, date_closed?)` | `date_closed` defaults to today + 30d |
| GET | `/Leads` | `list_leads(offset, max_num)` | |
| POST | `/Leads/{id}/convert` | `convert_lead(lead_id, modules)` | `modules = {Contacts: {...}, Accounts: {...}, Opportunities: {...}}` |
| GET | `/Meetings` | `list_meetings(offset, max_num)` | |

### Pagination

SugarCRM uses `?offset=<N>&max_num=<M>`; response includes `next_offset` (`-1` = end). The connector iterates until `next_offset < 0` in `sync()`.

### Filtering

SugarCRM accepts `filter=<JSON-encoded list of per-field predicates>` as a single querystring value. `sync()` injects `[{"date_modified": {"$gt": since.isoformat()}}]` for incremental runs.

## 6. Error Handling

| HTTP | Exception | Behaviour |
|---|---|---|
| 401 | `SugarCRMAuthError` | `refresh_and_retry_on_401` triggers `on_token_refresh()` + retries once; second 401 propagates |
| 403 | `SugarCRMError` | propagates; health-check classifies as `UNHEALTHY + INVALID_CREDENTIALS` |
| 404 | `SugarCRMError` | propagates to caller |
| 429 | `SugarCRMRateLimitError(retry_after)` | `with_retry` honours `Retry-After` on the first attempt then exponential backoff |
| 5xx | `SugarCRMNetworkError` | `with_retry` exponential backoff (max 3 retries, 32s cap) |
| transport | `SugarCRMNetworkError` | same as 5xx |

OCP — the `_STATUS_MAP` on `SugarCRMConnector` codifies HTTP-status → (`ConnectorHealth`, `AuthStatus`) so `health_check()` and `sync()` error paths never inline conditionals.

## 7. Dependencies

```
httpx>=0.27,<1.0
structlog>=24.1
pytest>=7
pytest-asyncio>=0.23
pytest-mock>=3.10
respx>=0.21
```

The shared `shielva-connectors/core` package supplies `BaseConnector`, `TokenInfo`, `RefreshError`, `NormalizedDocument`, and the Redis-backed token-store glue.

## 8. Config & Install Fields

Read via `self.config.get(...)` in `connector.py::__init__` — never hardcoded, never from `os.environ`.

| Key | Type | Required | Default | Purpose |
|---|---|---|---|---|
| `site_url` | string | yes | — | Root URL of SugarCRM deployment; `/rest/v11` is appended |
| `username` | string | yes (password grant) | — | Service-account username |
| `password` | secret | yes (password grant) | — | Service-account password |
| `client_id` | string | no | `sugar` | OAuth2 client; `sugar` is Sugar's built-in trusted client |
| `client_secret` | secret | no | `""` | OAuth2 client secret (custom OAuth apps only) |
| `grant_type` | string | no | `password` | `password` or `authorization_code` |
| `platform` | string | no | `api` | SugarCRM `platform` string sent with every token request |
| `redirect_uri` | string | no | `""` | Required when `grant_type=authorization_code` |
| `sync_modules` | array | no | `["Contacts","Accounts","Opportunities"]` | Modules to sync into the KB |
| `rate_limit_per_min` | number | no | 60 | Client-side soft cap |

### Class constants

```python
CONNECTOR_TYPE       = "sugarcrm"
CONNECTOR_NAME       = "SugarCRM"
AUTH_TYPE            = "oauth2_password"
REQUIRED_CONFIG_KEYS = ["site_url"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

### SOC (Separation Of Concerns)

| Module | Responsibility |
|---|---|
| `connector.py` | Lifecycle (install/authorize/health_check/sync) + public CRM methods (orchestration only) |
| `client/http_client.py` | All HTTP I/O — owns `httpx.AsyncClient`, exception mapping, token-endpoint POST |
| `helpers/normalizer.py` | Record → `NormalizedDocument` mapping (per module) |
| `helpers/utils.py` | Retry + backoff (`with_retry`) and single-shot 401 refresh-and-retry (`refresh_and_retry_on_401`) |
| `models.py` | `InstallResult` / `HealthCheckResult` / `SyncResult` + `auth_status` / `health` shims |
| `exceptions.py` | `SugarCRMError` hierarchy (Auth / Network / RateLimit) |

`connector.py` **never** calls `httpx` directly. `client/http_client.py` **never** imports `connector`. The dependency graph is a strict DAG: `connector` → `client` + `helpers` + `models` + `exceptions`.

### OCP (Open / Closed)

Future SugarCRM modules (Tasks, Calls, Documents, Notes, Quotes, Users) plug in by:

1. Adding the wrapper method to `connector.py` (e.g. `list_tasks`).
2. Adding the matching `normalize_X` to `helpers/normalizer.py` and registering it in `_NORMALIZERS`.
3. Adding the API row to `metadata/connector.json`.
4. Adding the matching `connector_docs.json` section.

No changes to the HTTP client, retry helpers, exception hierarchy, or `_STATUS_MAP`.

### Multi-tenant compliance

- `NormalizedDocument.id` is always `f"{tenant_id}_{source_id}"`.
- `connector_id` / `tenant_id` come from the `BaseConnector` constructor only — never derived from request context inside the connector.
- Secrets (`password`, `client_secret`) are sealed by the Shielva credential manager before storage; this connector receives them already decrypted via `self.config`.
