# Kommo Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Kommo** (formerly amoCRM) is a CRM platform exposing a REST API per-tenant
at a subdomain-scoped base URL: `https://{subdomain}.kommo.com/api/v4`. This
connector — `KommoConnector` (`CONNECTOR_TYPE = "kommo"`,
`AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant
typically needs from a Kommo account using a long-lived OAuth access token
treated as an API key:

| Surface | Base path | Capability |
|---|---|---|
| Account | `/account` | Health probe; verify token + subdomain. |
| Leads | `/leads` | List, get, create, update, delete leads (the deal pipeline). |
| Contacts | `/contacts` | List, get, create, update, delete contacts. |
| Companies | `/companies` | List, get, create, update, delete companies. |
| Customers | `/customers` | List customers (repeat-buyer CRM). |
| Tasks | `/tasks` | List, get, create, update, delete tasks. |
| Events | `/events` | List CRM events (timeline). |
| Notes | `/leads/{id}/notes`, etc. | List, create notes against any entity. |
| Pipelines | `/leads/pipelines` | List pipelines + statuses. |
| Custom Fields | `/{entity_type}/custom_fields` | List, create custom fields. |
| Users | `/users` | List operator users. |
| Webhooks | `/webhooks` | Create + delete outbound webhooks. |

The connector normalises leads + contacts into `NormalizedDocument`
(id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per
user-requested operation (OCP), routes via a single `KommoHTTPClient` that
owns httpx + retry, and never embeds raw HTTP in `connector.py` (SOC).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Kommo REST API uses **long-lived OAuth access token authentication** for
server-to-server integrations — Shielva treats this token as an API key
(`AUTH_TYPE = "api_key"`). The token is issued out-of-band by the operator from
Kommo Settings → Integrations → Long-Lived Token, scoped to the account.

### Credentials
- `subdomain` — the `{subdomain}` segment in `https://{subdomain}.kommo.com`.
  install_field (type `text`, required). Used to construct the per-tenant base
  URL `https://{subdomain}.kommo.com/api/v4`.
- `access_token` — the long-lived OAuth access token. install_field
  (type `secret`, required). Sent as `Authorization: Bearer <access_token>`.

### Header contract
Every request to `https://{subdomain}.kommo.com/api/v4/*`:

```
Authorization: Bearer <access_token>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `subdomain` and `access_token` are non-empty,
  sanitises the subdomain (strips protocol / `.kommo.com` suffix), persists
  via `save_config`. Does **not** call the API.
- `authorize()` — wrapper that returns a `TokenInfo` whose `access_token` is
  the configured long-lived token. No code-exchange step.
- `health_check()` — `GET /api/v4/account` as a lightweight probe.
- `ensure_token()` — N/A (long-lived token, no refresh).

## 4. Data Model

### 4.1 Lead → NormalizedDocument

| NormalizedDocument | Kommo JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{lead['id']}"` | tenant-scoped |
| `source_id` | `str(lead["id"])` | Kommo lead numeric id |
| `title` | `lead["name"]` or `f"Lead {id}"` | |
| `content` | concat name + price + pipeline + status | |
| `source` | `"kommo"` | |
| `source_url` | `https://{subdomain}.kommo.com/leads/detail/{id}` | |
| `created_at` | `datetime.fromtimestamp(lead["created_at"])` | epoch seconds |
| `updated_at` | `datetime.fromtimestamp(lead["updated_at"])` | |
| `metadata` | `{pipeline_id, status_id, price, responsible_user_id}` | |

### 4.2 Contact → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_contact_{contact['id']}"` |
| `source_id` | `str(contact["id"])` |
| `title` | `contact["name"]` or first+last |
| `content` | name |
| `source` | `"kommo"` |
| `source_url` | `https://{subdomain}.kommo.com/contacts/detail/{id}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST
exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; sanitise subdomain. |
| `authorize(auth_code, state)` | (lifecycle) | n/a | Wraps access_token in a TokenInfo. |
| `health_check()` | GET | `/account` | Lightweight account probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates leads | Calls `ingest_document`. |
| `list_leads(page, limit, query, filter)` | GET | `/leads` | Filters via bracket-style flatten. |
| `get_lead(lead_id)` | GET | `/leads/{id}` | |
| `create_lead(leads)` | POST | `/leads` | Array body. |
| `update_lead(lead_id, fields)` | PATCH | `/leads/{id}` | Single object body. |
| `delete_lead(lead_id)` | DELETE | `/leads/{id}` | |
| `list_contacts(page, limit, query)` | GET | `/contacts` | |
| `get_contact(contact_id)` | GET | `/contacts/{id}` | |
| `create_contact(contacts)` | POST | `/contacts` | Array body. |
| `update_contact(contact_id, fields)` | PATCH | `/contacts/{id}` | |
| `delete_contact(contact_id)` | DELETE | `/contacts/{id}` | |
| `list_companies(page, limit)` | GET | `/companies` | |
| `get_company(company_id)` | GET | `/companies/{id}` | |
| `create_company(companies)` | POST | `/companies` | Array body. |
| `update_company(company_id, fields)` | PATCH | `/companies/{id}` | |
| `delete_company(company_id)` | DELETE | `/companies/{id}` | |
| `list_customers(page, limit)` | GET | `/customers` | |
| `list_tasks(page, limit, filter)` | GET | `/tasks` | |
| `get_task(task_id)` | GET | `/tasks/{id}` | |
| `create_task(tasks)` | POST | `/tasks` | Array body. |
| `update_task(task_id, fields)` | PATCH | `/tasks/{id}` | |
| `delete_task(task_id)` | DELETE | `/tasks/{id}` | |
| `list_events(page, limit, filter)` | GET | `/events` | |
| `list_notes(entity_type, entity_id, page, limit)` | GET | `/{entity_type}/{id}/notes` | |
| `create_note(entity_type, entity_id, notes)` | POST | `/{entity_type}/{id}/notes` | Array body. |
| `list_custom_fields(entity_type)` | GET | `/{entity_type}/custom_fields` | |
| `create_custom_field(entity_type, fields)` | POST | `/{entity_type}/custom_fields` | Array body. |
| `list_pipelines()` | GET | `/leads/pipelines` | |
| `list_users()` | GET | `/users` | |
| `create_webhook(destination, settings)` | POST | `/webhooks` | |
| `delete_webhook(destination)` | DELETE | `/webhooks` | Body identifies by URL. |

Wire convention: Kommo uses **snake_case** in JSON and `_embedded` envelope
for list responses. The connector boundary returns raw `Dict[str, Any]`
payloads — callers reach into `_embedded[<entity>]`.

## 6. Error Handling

| HTTP | Kommo meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `KommoError` (raise) |
| 401 | Token invalid / revoked | `KommoAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (token lacks scope) | `KommoAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `KommoNotFound` (raise) |
| 429 | Rate limited (Kommo throttles ~7 req/sec) | `KommoError` (status_code=429) → `ConnectorHealth.DEGRADED`; retried with exponential backoff |
| 5xx | Provider outage | `KommoError` (status_code≥500) → retried |

All in `exceptions.py` extending `KommoError`. Back-compat alias
`KommoNetworkError = KommoError` for legacy import sites. Retry in
`client/http_client.py::_request` honours `max_retries=3`, exponential backoff
`min(1.0 * 2 ** attempt + jitter, 32.0)`.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27.0
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are
pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `subdomain` | text | yes | install_field | The `{subdomain}` in `{subdomain}.kommo.com` |
| `access_token` | secret | yes | install_field | Long-lived OAuth access token; sent as `Bearer <token>` |
| `base_url` | text | no | install_field | Optional override of `https://{subdomain}.kommo.com/api/v4` |
| `rate_limit_per_min` | number | no | install_field (default 100) | Client-side soft cap |
| `timeout_s` | number | no | install_field (default 30) | Per-request httpx timeout |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["subdomain", "access_token"]
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
| `client/http_client.py` | Single owner of httpx. Builds headers, retries 429/5xx, raises typed exceptions. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Kommo payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Subdomain sanitiser, with_retry helper. | `httpx`, `structlog`, `exceptions` |
| `models.py` | Pydantic schemas for Kommo entities. | `pydantic` |
| `exceptions.py` | `KommoError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `KommoConnector`. | `connector` |

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
