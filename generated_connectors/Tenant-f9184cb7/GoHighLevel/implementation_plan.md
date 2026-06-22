# GoHighLevel Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**GoHighLevel (HighLevel)** is an all-in-one CRM + marketing-automation + sales-enablement platform exposing a REST API under `https://services.leadconnectorhq.com` (the LeadConnector API surface, also reachable via the legacy `https://rest.gohighlevel.com/v1` host for v1 endpoints). This connector — `GoHighLevelConnector` (`CONNECTOR_TYPE = "gohighlevel"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a HighLevel sub-account / location:

| Surface | Base path | Capability |
|---|---|---|
| Locations | `/locations` | List + read sub-accounts (locations) |
| Contacts | `/contacts` | List, get, create, update, delete contacts |
| Opportunities | `/opportunities` | List, get, create, update opportunities (CRM pipeline) |
| Conversations | `/conversations` | List conversations + send messages (SMS/Email/IG/FB) |
| Calendars | `/calendars` | List calendars |
| Pipelines | `/opportunities/pipelines` | List sales pipelines + stages |
| Tags | `/locations/{id}/tags` | List location tags |
| Users | `/users` | List users in a location |
| Campaigns | `/campaigns` | List marketing campaigns |
| Custom Fields | `/locations/{id}/customFields` | List location custom fields |

The connector normalises contacts + opportunities + conversations into `NormalizedDocument` (id = `f"{connector_id}_{source_id}"`, tenant scoping via `self.tenant_id`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::GoHighLevelHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

No GoHighLevel-specific SDK is required — the public REST contract is stable and lightweight; building on `httpx` keeps the dependency surface minimal and consistent with the Wix/Bandwidth references.

## 3. Auth Flow

GoHighLevel exposes two compatible auth surfaces; this connector uses **API key (location-scoped Bearer token)** for server-to-server integrations.

### Credentials
- `api_key` — Location-level API key, copied from **HighLevel → Settings → Business Profile → API Key** (location) or **Agency Settings → API Key** (agency). Stored as install_field (type `secret`, required).
- `location_id` — Optional default location (sub-account) ID — sent as a `locationId` query/body field where applicable. install_field (type `text`, optional).
- `base_url` — Override (default `https://services.leadconnectorhq.com`). install_field (type `text`, optional).
- `api_version` — HighLevel `Version` header value, defaults to `2021-07-28`. install_field (type `text`, optional).

### Header contract
Every request:

```
Authorization: Bearer <api_key>
Version:        <api_version>            (e.g. 2021-07-28)
Content-Type:   application/json
Accept:         application/json
```

### Lifecycle
- `install()` validates `api_key` is non-empty. Does **not** call the API.
- `authorize()` returns a `TokenInfo` shell — there is no OAuth code exchange.
- `health_check()` — `GET /locations/{location_id}` when `location_id` set, else `GET /users/me` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Contact → NormalizedDocument

| NormalizedDocument | HighLevel JSON | Notes |
|---|---|---|
| `id` | `f"{connector_id}_{contact['id']}"` | tenant-scoped via connector_id |
| `source_id` | `contact["id"]` | HighLevel contact UUID |
| `title` | `contact["contactName"]` or first+last | |
| `content` | concat name + email + phone + tags | |
| `created_at` | `contact["dateAdded"]` | RFC 3339 |
| `updated_at` | `contact["dateUpdated"]` | |
| `metadata` | `{email, phone, tags, locationId, source, type}` | |

### 4.2 Opportunity → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{connector_id}_{opp['id']}"` |
| `source_id` | `opp["id"]` |
| `title` | `opp["name"]` |
| `content` | `f"Opportunity in stage {opp['pipelineStageId']} — {opp['status']}"` |
| `created_at` | `opp["createdAt"]` |
| `metadata` | `{status, monetaryValue, pipelineId, pipelineStageId, contactId, source}` |

### 4.3 Conversation Message → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{connector_id}_{conv['id']}"` |
| `source_id` | `conv["id"]` |
| `title` | `f"Conversation {conv['contactId']}"` |
| `content` | `conv.get("lastMessageBody", "")` |
| `metadata` | `{type, unreadCount, contactId, lastMessageType, lastMessageDate}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/locations/{id}` or `/users/me` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates contacts + opportunities + conversations | Calls `ingest_document`. |
| `list_locations(*, limit=20, skip=0)` | GET | `/locations/search?limit=&skip=` | Cursor not used; offset paging. |
| `get_location(location_id)` | GET | `/locations/{locationId}` | |
| `list_contacts(*, location_id=None, limit=20, page=1, query=None)` | GET | `/contacts/?locationId=&limit=&page=&query=` | Page paging. |
| `get_contact(contact_id)` | GET | `/contacts/{contactId}` | |
| `create_contact(payload)` | POST | `/contacts/` | Body: full contact JSON. |
| `update_contact(contact_id, payload)` | PUT | `/contacts/{contactId}` | |
| `delete_contact(contact_id)` | DELETE | `/contacts/{contactId}` | |
| `list_opportunities(*, location_id=None, pipeline_id=None, limit=20, page=1)` | GET | `/opportunities/search?...` | |
| `get_opportunity(opportunity_id)` | GET | `/opportunities/{opportunityId}` | |
| `create_opportunity(payload)` | POST | `/opportunities/` | |
| `update_opportunity(opportunity_id, payload)` | PUT | `/opportunities/{opportunityId}` | |
| `list_conversations(*, location_id=None, contact_id=None, limit=20)` | GET | `/conversations/search?...` | |
| `get_conversation(conversation_id)` | GET | `/conversations/{conversationId}` | |
| `send_message(conversation_id, payload)` | POST | `/conversations/messages` | Body: `{conversationId, type, message, contactId}`. |
| `list_calendars(*, location_id=None)` | GET | `/calendars/?locationId=` | |
| `list_pipelines(*, location_id=None)` | GET | `/opportunities/pipelines?locationId=` | |
| `list_users(*, location_id=None)` | GET | `/users/?locationId=` | |
| `list_campaigns(*, location_id=None)` | GET | `/campaigns/?locationId=` | |
| `list_custom_fields(location_id)` | GET | `/locations/{locationId}/customFields` | |
| `list_tags(location_id)` | GET | `/locations/{locationId}/tags` | |

Wire convention: HighLevel uses **camelCase** in JSON (`locationId`, `contactName`, `pipelineStageId`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | HighLevel meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `GoHighLevelError` (raise) |
| 401 | API key invalid / missing | `GoHighLevelAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (key lacks scope) | `GoHighLevelAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `GoHighLevelNotFound` (raise) |
| 422 | Validation error | `GoHighLevelError` (raise) |
| 429 | Rate limited | `GoHighLevelError` → retry exponential backoff |
| 5xx | Provider outage | `GoHighLevelNetworkError` → retry with exponential backoff |

All in `exceptions.py` extending `GoHighLevelError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `0.5 * 2 ** attempt` (0.5s, 1s, 2s) for 429 + 5xx.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
(none beyond pre-installed httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx)
```

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `Authorization: Bearer <key>` header value |
| `base_url` | text | no | install_field (default `https://services.leadconnectorhq.com`) | Override host |
| `location_id` | text | no | install_field | Default location for scoped operations |
| `api_version` | text | no | install_field (default `2021-07-28`) | `Version` header value |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_key"]
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
| `helpers/normalizer.py` | Maps raw HighLevel payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | with_retry async helper, safe_get nested lookup. | (stdlib only) |
| `models.py` | Pydantic schemas with camelCase aliases for request bodies. | `pydantic` |
| `exceptions.py` | `GoHighLevelError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `GoHighLevelConnector`. | `connector` |

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
