# EngageBay Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**EngageBay** is an all-in-one SMB CRM, marketing, sales, service, and helpdesk SaaS. It exposes a REST API under `https://app.engagebay.com/dev/api/panel`. This connector — `EngageBayConnector` (`CONNECTOR_TYPE = "engagebay"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs:

| Surface | Base path | Capability |
|---|---|---|
| Contacts (CRM) | `/contacts`, `/contacts/{id}`, `/contacts/update-partial/{id}` | List, get, create, update, delete |
| Companies | `/companies/list/{page_size}` | List companies |
| Deals | `/deals`, `/deals/{id}` | List, create, get |
| Tasks | `/tasks`, `/tasks/{id}` | List, create, get |
| Tickets | `/tickets` | List support tickets |
| Notes | `/contacts/{id}/note` | Attach free-text note to a contact |
| Subusers | `/subusers/list` | Lightweight auth probe (health check) |

The connector normalizes contacts + deals + tasks into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff + jitter (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::EngageBayHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

EngageBay REST API uses **API key authentication** for server-to-server integrations.

### Credentials
- `api_key` — generated under EngageBay → Account → Account Settings → API Settings → REST API Key. Stored as install_field (type `secret`, required).
- `base_url` — optional override for sandbox/self-hosted (default `https://app.engagebay.com/dev/api/panel`).
- `rate_limit_per_min` — optional soft cap (default 60).

### Header contract
Every request:

```
Authorization: <api_key>           ← raw, no Bearer prefix (EngageBay-specific)
Content-Type:   application/json
Accept:         application/json
```

### Lifecycle
- `install()` — validates `api_key` is non-empty and probes `GET /subusers/list`. Returns `ConnectorStatus(HEALTHY, CONNECTED)` on success.
- `authorize(auth_code, state)` — returns the stored key as a `TokenInfo`.
- `health_check()` — calls `GET /subusers/list` as a lightweight probe.

## 4. Data Model

### 4.1 Contact → NormalizedDocument

| NormalizedDocument | EngageBay JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{contact['id']}"` | tenant-scoped |
| `source_id` | `str(contact["id"])` | EngageBay contact id |
| `title` | `first_name + " " + last_name` or email | flattened from `properties[]` |
| `content` | name + email + phone | from flat properties |
| `metadata` | `{email, phone, company, tags, properties}` | |

### 4.2 Deal → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{deal['id']}"` |
| `source_id` | `str(deal["id"])` |
| `title` | `deal["name"]` |
| `content` | `name + milestone + expected_value` |
| `metadata` | `{expected_value, milestone, pipeline_id, contact_ids}` |

### 4.3 Task → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{task['id']}"` |
| `source_id` | `str(task["id"])` |
| `title` | `task["name"]` |
| `content` | `name + status` |
| `metadata` | `{due_date, owner_id, status}` |

## 5. Key API Endpoints & Methods

Every method below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate `api_key`; probe `/subusers/list`. |
| `authorize()` | (lifecycle) | n/a | Returns TokenInfo over `api_key`. |
| `health_check()` | GET | `/subusers/list` | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates `/contacts` cursor pages | Ingests `NormalizedDocument`. |
| `list_contacts(page_size, page_cursor)` | GET | `/contacts` | Cursor pagination. |
| `get_contact(contact_id)` | GET | `/contacts/{id}` | Returns flat normalized dict. |
| `create_contact(properties)` | POST | `/contacts` | Body: `{properties: [...]}`. |
| `update_contact(contact_id, properties)` | PUT | `/contacts/update-partial/{id}` | Partial update. |
| `delete_contact(contact_id)` | DELETE | `/contacts/{id}` | |
| `list_companies(page_size)` | GET | `/companies/list/{page_size}` | Path-segment paging. |
| `list_deals(page_size)` | GET | `/deals` | |
| `create_deal(name, expected_value, milestone, contact_ids)` | POST | `/deals` | |
| `list_tasks(page_size)` | GET | `/tasks` | |
| `create_task(name, due_date, contact_id, owner_id)` | POST | `/tasks` | `due_date` is epoch ms. |
| `list_tickets(page_size)` | GET | `/tickets` | |
| `add_note(contact_id, note)` | POST | `/contacts/{id}/note` | Free-text note attach. |

Wire convention: EngageBay uses **snake_case** JSON (`first_name`, `expected_value`, `due_date`). The connector boundary returns these as-is in `Dict[str, Any]` payloads; `get_contact()` additionally flattens the `properties[]` array.

## 6. Error Handling

| HTTP | EngageBay meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `EngageBayError` (raise) |
| 401 | API key invalid / missing header | `EngageBayAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (key lacks permission) | `EngageBayAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `EngageBayNotFound` (raise) |
| 429 | Rate limited (honours `Retry-After`) | retried with exponential backoff + jitter |
| 5xx | Provider outage | retried with exponential backoff |
| transport (DNS/timeout/reset) | network | `EngageBayNetworkError` |

All exceptions in `exceptions.py` extending `EngageBayError`. Retry budget: `client/http_client.py::_request` retries `429/500/502/503/504` up to `max_retries=3` with backoff `min(8s, 2**attempt * 0.5) + jitter`.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27.0
```

(structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `Authorization` header value (raw, no Bearer) |
| `base_url` | string | no | install_field (default `https://app.engagebay.com/dev/api/panel`) | Override for sandbox |
| `rate_limit_per_min` | number | no | install_field (default 60) | Soft client-side cap |

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
| `helpers/normalizer.py` | Maps raw EngageBay payloads → `NormalizedDocument` + flat-dict helpers. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry`, `require` argument validation. | `structlog`, `exceptions` |
| `models.py` | Light dataclasses + local enum mirrors for in-isolation tooling. | (stdlib) |
| `exceptions.py` | `EngageBayError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `EngageBayConnector`. | `connector` |

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
