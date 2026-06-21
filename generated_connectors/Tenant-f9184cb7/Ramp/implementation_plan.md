# Ramp Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Ramp** is a corporate-card and spend-management platform exposing a REST API under `https://api.ramp.com/developer/v1`. This connector — `RampConnector` (`CONNECTOR_TYPE = "ramp"`, `AUTH_TYPE = "oauth2_client_credentials"`) — wraps the operational surfaces a Shielva tenant typically needs from Ramp:

| Surface | Base path | Capability |
|---|---|---|
| Users | `/users`, `/users/deferred` | List, get, invite users |
| Cards | `/cards` | List, get cards |
| Transactions | `/transactions` | List, get card transactions |
| Departments | `/departments` | List departments |
| Locations | `/locations` | List locations |
| Reimbursements | `/reimbursements` | List reimbursement requests |
| Bills | `/bills` | List bills (AP) |
| Vendors | `/vendors` | List vendors |
| Limits | `/limits` | List spend limits |
| Memos | `/memos` | List, get memos |

The connector normalises users + transactions into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), and routes auth + retry through `client/http_client.py::RampHTTPClient` (SOC — no HTTP in `connector.py`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator for transient errors |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Ramp Developer API uses **OAuth2 client_credentials** for server-to-server integrations.

### Credentials
- `client_id` — OAuth2 client_id from Ramp Dashboard → Developer → API. install_field (type `string`, required).
- `client_secret` — OAuth2 client_secret from Ramp Dashboard → Developer → API. install_field (type `secret`, required, shown once).
- `scopes` — Space-separated scopes (optional; default set baked into the connector).

### Token grant
The HTTP client mints an access_token via:

```
POST https://api.ramp.com/developer/v1/token
Authorization: Basic base64(client_id:client_secret)
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=client_credentials&scope=<space-separated>
```

Response:
```json
{ "access_token": "...", "token_type": "Bearer", "expires_in": 3600, "scope": "..." }
```

### Lifecycle
- `install()` validates `client_id` + `client_secret` are non-empty, then **probes the token endpoint**. On 401 → `INVALID_CREDENTIALS + UNHEALTHY`. On 5xx/network → `PENDING + DEGRADED`. On success → `AUTHENTICATED + HEALTHY`.
- `authorize()` — runs the client_credentials grant and returns a `TokenInfo` (no auth-code dance for this grant type).
- `health_check()` — `GET /users?page_size=1` as a lightweight probe.
- `ensure_token()` — handled inside `RampHTTPClient._get_token()` with a 60 s safety margin; on 401 the cache is invalidated and a fresh token is minted exactly once before the request is retried.

### Header contract
Every API request:
```
Authorization: Bearer <access_token>
Content-Type:   application/json
Accept:         application/json
Idempotency-Key: <uuid>   (POST mutations only)
```

## 4. Data Model

### 4.1 User → NormalizedDocument

| NormalizedDocument | Ramp JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{user['id']}"` | tenant-scoped |
| `source_id` | `user["id"]` | Ramp user UUID |
| `title` | `f"{first_name} {last_name}"` or `email` | |
| `content` | `email` | |
| `author` | `email` | |
| `created_at` | `user["created_at"]` | RFC 3339 |
| `metadata` | `{role, email, department_id, location_id, status, kind: "ramp.user"}` | |

### 4.2 Transaction → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{tx['id']}"` |
| `source_id` | `tx["id"]` |
| `title` | `f"{merchant_name} ({currency_code} {amount})"` |
| `content` | `tx.get("memo", "")` or category name |
| `created_at` | `tx["user_transaction_time"]` |
| `metadata` | `{amount, currency_code, merchant_name, card_id, user_id, sk_category_id, sk_category_name, kind: "ramp.transaction"}` |

### 4.3 Card → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{card['id']}"` |
| `source_id` | `card["id"]` |
| `title` | `card["display_name"]` |
| `metadata` | `{user_id, is_physical, state, spending_restrictions, kind: "ramp.card"}` |

## 5. Key API Endpoints & Methods

Every method below exists as a standalone public `async def` in `connector.py` (OCP).

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate + probe token endpoint. |
| `authorize()` | POST | `/token` | client_credentials grant. |
| `health_check()` | GET | `/users?page_size=1` | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates `/users` + `/transactions` | Calls `ingest_document`. |
| `list_users(*, department_id, location_id, role, start, page_size=50)` | GET | `/users` | Cursor pagination via `start`. |
| `get_user(user_id)` | GET | `/users/{id}` | |
| `invite_user(first_name, last_name, email, role=BUSINESS_USER, department_id, location_id, idempotency_key)` | POST | `/users/deferred` | Idempotency-Key header. |
| `list_cards(*, user_id, start, page_size=50, is_physical)` | GET | `/cards` | |
| `get_card(card_id)` | GET | `/cards/{id}` | |
| `list_transactions(*, start, end, sk_category_id, merchant_id, page_size=50)` | GET | `/transactions` | `start`/`end` map to `from_date`/`to_date` query params. |
| `get_transaction(transaction_id)` | GET | `/transactions/{id}` | |
| `list_departments(*, start, page_size=50)` | GET | `/departments` | |
| `list_locations(*, start, page_size=50)` | GET | `/locations` | |
| `list_reimbursements(*, start, page_size=50, user_id)` | GET | `/reimbursements` | |
| `list_bills(*, page_size=50, start)` | GET | `/bills` | |
| `list_vendors(*, page_size=50, start)` | GET | `/vendors` | |
| `list_limits(*, page_size=50, start, user_id)` | GET | `/limits` | |
| `list_memos(*, page_size=50, start)` | GET | `/memos` | |
| `get_memo(memo_id)` | GET | `/memos/{id}` | |

Wire convention: Ramp uses **snake_case** in JSON (`first_name`, `user_transaction_time`, `sk_category_id`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Ramp meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `RampBadRequestError` (raise) |
| 401 | Access token expired / invalid | `RampAuthError` — connector refreshes token once, then surfaces |
| 403 | Forbidden (scopes insufficient) | `RampAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `RampNotFoundError` (raise) |
| 409 | Conflict (e.g. duplicate idempotency key with different body) | `RampConflictError` |
| 429 | Rate limited (Ramp may emit `Retry-After`) | `RampRateLimitError` → backoff respects header, `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `RampServerError` → retry with exponential backoff |

All in `exceptions.py` extending `RampError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff starting at 0.5 s for 5xx, honouring `Retry-After` for 429. Back-compat aliases preserved (`RampNetworkError = RampServerError`, `RampNotFound = RampNotFoundError`).

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads `requirements.txt`):

```
httpx>=0.27,<1.0
pydantic>=2.0
structlog>=24.1
tenacity>=8.2
```

(pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `client_id` | string | yes | install_field | OAuth2 client_id |
| `client_secret` | secret | yes | install_field | OAuth2 client_secret |
| `scopes` | string | no | install_field | Space-separated scope list |
| `base_url` | string | no | install_field (default `https://api.ramp.com/developer/v1`) | Override for sandbox/proxy |
| `token_url` | string | no | install_field (default `https://api.ramp.com/developer/v1/token`) | Override only if Ramp directs |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["client_id", "client_secret"]
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
| `client/http_client.py` | Single owner of httpx + OAuth2 token cache + retry. Builds headers, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Ramp payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry`, `safe_get`, `clean_params`. | (stdlib only) |
| `models.py` | Pydantic schemas with snake_case aliases for request bodies. | `pydantic` |
| `exceptions.py` | `RampError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `RampConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, pagination, token cache) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
