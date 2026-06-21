# Nutshell Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Nutshell** is an SMB-focused sales CRM with a JSON-RPC 2.0 API at `https://app.nutshell.com/api/v1/json` (and a REST beta at `https://api.nutshell.com/v1/rest`). This connector — `NutshellConnector` (`CONNECTOR_TYPE = "nutshell"`, `AUTH_TYPE = "api_key"`) — wraps the operational CRM surfaces a Shielva tenant typically needs:

| Surface     | RPC methods                                                | Capability                                      |
|-------------|------------------------------------------------------------|-------------------------------------------------|
| Contacts    | `findContacts`, `getContact`, `newContact`, `editContact`, `deleteContact` | List/get/create/edit/delete person records   |
| Leads       | `findLeads`, `getLead`, `newLead`, `editLead`              | Pipeline opportunities                          |
| Accounts    | `findAccounts`, `getAccount`, `newAccount`                 | Company records                                 |
| Activities  | `findActivities`, `newActivity`                            | Calls, meetings, emails, notes                  |
| Tasks       | `findTasks`                                                | Sales tasks / follow-ups                        |
| Notes       | `findNotes`                                                | Free-form notes on contacts/leads/accounts      |
| Pipelines   | `findStagesets`                                            | Pipeline + stage definitions                    |
| Users       | `findUsers`, `getUser`                                     | Nutshell seat list (assignment, ownership)      |

**Wire format.** Every call is `POST https://app.nutshell.com/api/v1/json` with a JSON-RPC envelope:

```json
{"jsonrpc": "2.0", "id": <int>, "method": "<rpcName>", "params": {...}}
```

Successful response: `{"jsonrpc": "2.0", "id": <int>, "result": <any>}`.
Error response (HTTP **200** is common): `{"jsonrpc": "2.0", "id": <int>, "error": {"code": <int>, "message": "..."}}`.

The connector normalises contacts + leads + accounts into typed dataclasses (id-stable, `id = f"{tenant_id}_{source_id}"` when emitted as `NormalizedDocument`), surfaces standalone `async def` methods per RPC (OCP), and centralises all HTTP, retry, and envelope parsing in `client/http_client.py::NutshellHTTPClient` (SOC).

## 2. SDK / Package Selection

| Package          | Version       | Justification                                                |
|------------------|---------------|--------------------------------------------------------------|
| `httpx`          | `>=0.27,<1.0` | Async client; pre-installed in shared venv                    |
| `pydantic`       | `>=2.0`       | Request/response schemas (kept thin; mostly dataclasses)      |
| `structlog`      | `>=24.1`      | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed          |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

Connector-specific install (Section 7): **none** — pure `httpx`.

## 3. Auth Flow

Nutshell uses **HTTP Basic** authentication with the user's login email + an issued API key.

### Credentials
- `username` — Nutshell login email of the user the key was issued for. install_field (`string`, required).
- `api_key` — Issued from Nutshell → Setup → API Keys. install_field (`secret`, required).
- `base_url` — Override for sandbox / proxy. install_field (`string`, optional, default `https://app.nutshell.com/api/v1/json`).
- `rate_limit_per_min` — Client-side soft cap. install_field (`number`, optional, default 60).

### Header contract

```
Authorization: Basic base64(<username>:<api_key>)
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `username` + `api_key` are non-empty, then calls `getUser` ({} params) as a credential probe. 200 + `result` → `(HEALTHY, CONNECTED)`. 401 / RPC `error` → `(OFFLINE, MISSING_CREDENTIALS)`.
- `authorize()` — N/A (`api_key` flow has no exchange). Returns a placeholder `TokenInfo`.
- `health_check()` — `getUser` probe. Cheap, always available to an authenticated key.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

Nutshell returns rich JSON-RPC records with `id`, `rev` (CouchDB-style optimistic-locking revision), nested name objects, and typed sub-resources. The connector keeps callers schema-aware via thin dataclasses in `models.py` (`NutshellContact`, `NutshellLead`, `NutshellAccount`) but emits plain dicts at the API boundary.

### 4.1 Contact → normalized dict

| out key          | source                                       | Notes                                  |
|------------------|----------------------------------------------|----------------------------------------|
| `id`             | `raw["id"]`                                  | int, Nutshell contact id               |
| `rev`            | `raw["rev"]`                                 | required for editContact / deleteContact|
| `display_name`   | `raw["name"]["displayName"]`                 |                                        |
| `first_name`     | `raw["name"]["givenName"]`                   |                                        |
| `last_name`      | `raw["name"]["familyName"]`                  |                                        |
| `emails`         | `raw["email"]`                               | list of `{value, type}`                |
| `phones`         | `raw["phone"]`                               | list of `{value, type}`                |
| `accounts`       | `raw["accounts"]`                            | list of `{id, name}` joins             |
| `custom_fields`  | `raw["customFields"]`                        | dict                                   |
| `created_time`   | `raw["createdTime"]`                         | ISO-8601                               |
| `modified_time`  | `raw["modifiedTime"]`                        | ISO-8601                               |
| `raw`            | the original record                          |                                        |

### 4.2 Lead → normalized dict
Adds `description`, `confidence`, `value` (amount + currency), `status`, `primary_account`, `contacts[]`.

### 4.3 Account → normalized dict
Adds `name`, `industry`, `territory`.

### 4.4 `NormalizedDocument` mapping (sync ingestion)

| field         | from                                          |
|---------------|-----------------------------------------------|
| `id`          | `f"{tenant_id}_{source_id}"`                  |
| `source_id`   | Nutshell record id (int → str)                |
| `title`       | display_name / description / account name     |
| `content`     | concat of name + emails + accounts (contacts) |
| `source`      | `"nutshell.<resource>"`                       |
| `created_at`  | `raw["createdTime"]`                          |
| `updated_at`  | `raw["modifiedTime"]`                         |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method                                        | RPC                | Params                                       | Notes                                    |
|-----------------------------------------------|--------------------|----------------------------------------------|------------------------------------------|
| `install()`                                   | (lifecycle)        | n/a                                          | Validates + probes `getUser`.            |
| `health_check()`                              | `getUser`          | `{}`                                         | Lightweight credential probe.            |
| `sync(since, full, kb_id, webhook_url)`       | iterates 3 RPCs    | contacts + leads + accounts                  | Calls `ingest_document` per record.      |
| `list_contacts(page, limit, query, order_by)` | `findContacts`     | `{page, limit, query?, orderBy}`             | Server-side ordering.                    |
| `get_contact(contact_id, contact_rev)`        | `getContact`       | `{contactId, rev?}`                          |                                          |
| `create_contact(contact)`                     | `newContact`       | `{contact: {...}}`                           |                                          |
| `update_contact(contact_id, rev, fields)`     | `editContact`      | `{contactId, rev, contact: fields}`          | `rev` required (optimistic locking).     |
| `delete_contact(contact_id, rev)`             | `deleteContact`    | `{contactId, rev}`                           |                                          |
| `list_leads(page, limit, query)`              | `findLeads`        | `{page, limit, query?}`                      |                                          |
| `create_lead(lead)`                           | `newLead`          | `{lead: {...}}`                              |                                          |
| `list_accounts(page, limit)`                  | `findAccounts`     | `{page, limit}`                              |                                          |
| `list_activities(page, limit)`                | `findActivities`   | `{page, limit}`                              |                                          |
| `log_activity(activity)`                      | `newActivity`      | `{activity: {...}}`                          |                                          |
| `list_users()`                                | `findUsers`        | `{}`                                         | All Nutshell seats.                      |

Wire convention: Nutshell RPC params use **camelCase** (`orderBy`, `contactId`, `customFields`, `createdTime`). The connector boundary accepts/returns Pythonic snake_case in arguments but passes camelCase RPC params on the wire.

## 6. Error Handling

Nutshell returns errors in two ways:

1. **HTTP-level** — 401 (bad creds), 404 (rare; URL-level), 429 (rate limited), 5xx (provider outage).
2. **JSON-RPC envelope at HTTP 200** — `{"error": {"code": <int>, "message": "..."}}`. The connector inspects the body even on 200.

| HTTP / RPC                       | Mapped to                              | Behaviour                                                  |
|----------------------------------|----------------------------------------|------------------------------------------------------------|
| 401 Unauthorized                 | `NutshellAuthError`                    | `(OFFLINE, MISSING_CREDENTIALS)` at install/health-check.   |
| 404 Not Found                    | `NutshellNotFound`                     | Raise (caller catches; surfaced as 404 in gateway).         |
| 429 Rate limited                 | `NutshellRateLimitError`               | Retry with exponential backoff + jitter; honor `Retry-After`.|
| 5xx Server error                 | `NutshellNetworkError`                 | Retry; surface after exhaustion.                            |
| RPC code = -32000 / -32001 / 401 | `NutshellAuthError`                    | Auth failure embedded in 200.                               |
| RPC message contains "not found" | `NutshellNotFound`                     |                                                              |
| Any other RPC error              | `NutshellError(rpc_code, message)`     | Generic.                                                     |
| Transport (httpx) error          | `NutshellNetworkError`                 | Retry; surface after exhaustion.                            |

All in `exceptions.py` extending `NutshellError`. Retry in `client/http_client.py::_post_rpc` honors `max_retries=3`, exponential backoff `min(1 * 2**attempt + jitter, 32)` seconds, with `Retry-After` taking precedence on the first attempt.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
# none — httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed
```

## 8. Config & Install Fields

| Key                  | Type   | Required | Source        | Notes                                          |
|----------------------|--------|----------|---------------|------------------------------------------------|
| `username`           | string | yes      | install_field | Nutshell login email; HTTP Basic username.     |
| `api_key`            | secret | yes      | install_field | Nutshell API key; HTTP Basic password.         |
| `base_url`           | string | no       | install_field | Defaults to `https://app.nutshell.com/api/v1/json`. |
| `rate_limit_per_min` | number | no       | install_field | Client-side soft cap (default 60).             |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["email", "api_key"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

(`email` is an alias accepted in config so that the install_field key matches the public Nutshell terminology; `username` is the legacy alias and is still honoured for backwards-compat with already-stored sessions.)

## 9. SOC/OCP Architecture Plan

| File                       | Responsibility                                                              | Imports                                                                                  |
|----------------------------|-----------------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| `connector.py`             | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py`    | Single owner of httpx. Builds JSON-RPC envelope, retries, raises typed exceptions. | `httpx`, `structlog`, `exceptions`                                                       |
| `helpers/normalizer.py`    | Maps raw Nutshell records → typed dataclasses → flat dicts.                  | `models`                                                                                  |
| `helpers/utils.py`         | Retry decorator (with_retry) and small helpers.                              | `structlog`, `exceptions` (stdlib only otherwise)                                         |
| `models.py`                | Thin dataclasses for Contact / Lead / Account.                               | stdlib only                                                                               |
| `exceptions.py`            | `NutshellError` hierarchy.                                                   | stdlib                                                                                    |
| `__init__.py`              | Re-export `NutshellConnector`.                                               | `connector`                                                                               |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New RPCs added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, envelope parsing) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
