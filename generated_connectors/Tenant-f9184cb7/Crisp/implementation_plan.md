# Crisp Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Crisp** is a customer messaging + helpdesk platform exposing a REST API at `https://api.crisp.chat/v1`. This connector — `CrispConnector` (`CONNECTOR_TYPE = "crisp"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Crisp website:

| Surface | Base path | Capability |
|---|---|---|
| User / Account | `/user/account`, `/user/websites` | Authenticated profile + accessible websites |
| Website | `/website/{wid}` | Read a website's settings |
| Conversations | `/website/{wid}/conversations/{page}`, `/website/{wid}/conversation/{sid}` | List, get, send message, assign operator, change state |
| People (contacts) | `/website/{wid}/people/profiles/{page}`, `/website/{wid}/people/profile/{pid}` | List, get, create, update, delete |
| Helpdesk | `/website/{wid}/helpdesk/locale/{locale}/articles/{page}` | List helpdesk articles for KB sync |
| Campaigns | `/website/{wid}/campaigns/list/{page}` | List campaigns |

The connector normalises conversations + people + helpdesk articles into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429 / 5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::CrispHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async HTTP client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

The Crisp Python SDK exists but is sync and out of date — we use raw `httpx` to keep the async story clean and to control retry semantics.

## 3. Auth Flow

Crisp REST API uses **HTTP Basic authentication** for plugin-tier integrations.

### Credentials
- `identifier` — plugin identifier from **Crisp Marketplace → Plugin → Tokens**. install_field (type `text`, required).
- `api_key` — plugin secret key paired with the identifier. install_field (type `secret`, required).
- `website_id` — Crisp website UUID this connector operates on. install_field (type `text`, required).
- `tier` — API tier header value, almost always `plugin` (also `user` for personal tokens). install_field (type `text`, default `plugin`, optional).

### Header contract
Every request to `https://api.crisp.chat/v1/*`:

```
Authorization: Basic base64(identifier:api_key)
X-Crisp-Tier:  plugin                (or "user")
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` — validates `identifier`, `api_key`, `website_id` are non-empty. Persists config. Does NOT call the API.
- `authorize()` — NOT applicable (Basic auth, no token exchange). Returns a synthesised `TokenInfo` for ABI compatibility.
- `health_check()` — `GET /user/account` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Conversation → NormalizedDocument

| NormalizedDocument | Crisp JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{session_id}"` | tenant-scoped |
| `source_id` | `session_id` | Crisp session UUID |
| `title` | `meta.subject` or `preview` | |
| `content` | `last_message` or `preview` | |
| `source` | `"crisp.conversation"` | metadata.kind |
| `author` | `meta.email` or `meta.nickname` | |
| `created_at` | `created_at` (ms epoch) | parsed via `_ts_to_dt` |
| `updated_at` | `updated_at` (ms epoch) | |
| `metadata` | `{state, website_id, assigned, segments, tenant_id, kind:"crisp.conversation"}` | |

### 4.2 Person → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{people_id}"` |
| `source_id` | `people_id` |
| `title` | `person.nickname` or `email` |
| `content` | concat name + email + segments |
| `source` | `"crisp.person"` |
| `created_at` | `created_at` |
| `metadata` | `{email, segments, person, tenant_id, kind:"crisp.person"}` |

### 4.3 Helpdesk Article → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{article_id}"` |
| `source_id` | `article_id` |
| `title` | `title` |
| `content` | `content` or `description` |
| `source` | `"crisp.helpdesk"` |
| `created_at` | `created_at` |
| `metadata` | `{locale, category_id, visibility, tenant_id, kind:"crisp.helpdesk"}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/user/account` | Lightweight authenticated probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates conversations + people + helpdesk | Calls `ingest_document`. |
| `get_account_profile()` | GET | `/user/account` | Authenticated user / plugin profile. |
| `list_websites()` | GET | `/user/websites` | Websites accessible to the credential. |
| `get_website(website_id)` | GET | `/website/{wid}` | Read website settings. |
| `list_conversations(website_id, *, page=1, per_page=50, search_query=None, search_filter_type=None)` | GET | `/website/{wid}/conversations/{page}` | Paginated conversation list. |
| `get_conversation(website_id, session_id)` | GET | `/website/{wid}/conversation/{sid}` | Fetch one conversation. |
| `send_message(website_id, session_id, type, from_, origin, content)` | POST | `/website/{wid}/conversation/{sid}/message` | Body: `{type, from, origin, content}` — `from_` renamed to `from` on the wire. |
| `list_people(website_id, *, page=1, per_page=50, search_text=None, search_filter=None)` | GET | `/website/{wid}/people/profiles/{page}` | Paginated contact list. |
| `get_person(website_id, people_id)` | GET | `/website/{wid}/people/profile/{pid}` | Fetch one contact. |
| `create_person(website_id, email=None, person=None, segments=None)` | POST | `/website/{wid}/people/profile` | Body: `{email, person, segments}` — only non-None keys. |
| `update_person(website_id, people_id, person)` | PATCH | `/website/{wid}/people/profile/{pid}` | Body: `{person: {...}}`. |
| `list_helpdesks(website_id, *, locale="en", page=1)` | GET | `/website/{wid}/helpdesk/locale/{locale}/articles/{page}` | Helpdesk articles for a locale. |
| `list_campaigns(website_id, *, page=1)` | GET | `/website/{wid}/campaigns/list/{page}` | Campaigns list. |

Wire convention: Crisp uses **snake_case** in JSON (`session_id`, `people_id`, `last_message`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Crisp meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `CrispBadRequestError` (raise) |
| 401 | Plugin credentials invalid | `CrispAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden — key lacks scope | `CrispAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Resource not found | `CrispNotFound` (raise) |
| 409 | Conflict (duplicate person email) | `CrispConflictError` |
| 429 | Rate limited | `CrispRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `CrispServerError` → retry with exponential backoff |

All in `exceptions.py` extending `CrispError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt` (0.5s, 1s, 2s) for 5xx + 429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
# (no additional packages — httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx pre-installed)
```

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `identifier` | text | yes | install_field | Plugin identifier |
| `api_key` | secret | yes | install_field | Plugin key — Basic auth password half |
| `website_id` | text | yes | install_field | Default Crisp website UUID |
| `tier` | text | no | install_field (default `plugin`) | `X-Crisp-Tier` header value |
| `base_url` | text | no | install_field (default `https://api.crisp.chat/v1`) | Crisp REST API base URL |
| `rate_limit_per_min` | number | no | install_field (default `60`) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["identifier", "api_key", "website_id"]
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
| `helpers/normalizer.py` | Maps raw Crisp payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry helper, timestamp parsing, safe nested-dict access. | (stdlib only) |
| `models.py` | Pydantic schemas with `from_` → `from` alias for the message body. | `pydantic` |
| `exceptions.py` | `CrispError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `CrispConnector`. | `connector` |

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
