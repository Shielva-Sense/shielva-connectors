# Brex Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Brex** is a corporate-card + spend-management platform exposing a REST API at `https://platform.brexapis.com`. This connector — `BrexConnector` (`CONNECTOR_TYPE = "brex"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from Brex:

| Surface | Base path | Capability |
|---|---|---|
| Users | `/v2/users` | List + read users, current user lookup |
| Cards | `/v2/cards` | List + read corporate cards |
| Transactions | `/v2/transactions/card/primary` + `/v2/transactions/cash` | List + read card and cash transactions |
| Expenses | `/v1/expenses/card` | List + read expense records (receipts, categories) |
| Departments | `/v2/departments` | List org-tree departments |
| Locations | `/v2/locations` | List office/site locations |
| Vendors | `/v1/vendors` | List vendors (AP module) |
| Receipts | `/v1/expenses/card/receipt_match` | List receipts attached to expenses |
| Budgets | `/v2/budgets` | List budgets + spend limits |
| Spend Limits | `/v2/spend_limits` | List spend limits per program |

The connector normalises **transactions + expenses + users** into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::BrexHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator alternative to in-client backoff |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`.

## 3. Auth Flow

Brex REST API uses **Bearer token authentication** for server-to-server integrations. Tokens are minted via OAuth2 client credentials OR personal access tokens generated in the Brex Dashboard → Developer settings.

### Credentials
- `access_token` — Brex API access token. Stored as install_field (type `secret`, required).
- `base_url` — Override for sandbox / staging environments. install_field (type `text`, optional, default `https://platform.brexapis.com`).

### Header contract

```
Authorization: Bearer <access_token>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `access_token` is non-empty. Does **not** call the API.
- `authorize()` — NOT a code exchange (api_key flow); returns a surface-compatible `TokenInfo` with `access_token`.
- `health_check()` — `GET /v2/users/me` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle, no refresh).

## 4. Data Model

### 4.1 Transaction → NormalizedDocument

| NormalizedDocument | Brex JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{tx['id']}"` | tenant-scoped |
| `source_id` | `tx["id"]` | |
| `title` | `tx["description"]` or `f"Transaction {tx['id']}"` | |
| `content` | merchant + amount + posted_date | |
| `source` | `"brex.transactions"` | |
| `created_at` | `tx["posted_at_date"]` | RFC 3339 |
| `metadata` | `{amount_cents, currency, type, card_id, merchant_category_code}` | |

### 4.2 Expense → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{exp['id']}"` |
| `source_id` | `exp["id"]` |
| `title` | `exp["memo"]` or `f"Brex expense {exp['id']}"` |
| `content` | merchant + amount + category + memo |
| `source` | `"brex.expenses"` |
| `created_at` | `exp["purchased_at"]` |
| `metadata` | `{amount_cents, currency, category, status, payment_status, expense_type}` |

### 4.3 User → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{user['id']}"` |
| `source_id` | `user["id"]` |
| `title` | `f"{user['first_name']} {user['last_name']}"` |
| `content` | email + department + status |
| `source` | `"brex.users"` |
| `metadata` | `{email, status, role, department_id, location_id}` |

## 5. Key API Endpoints & Methods

Every method listed below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config. |
| `health_check()` | GET | `/v2/users/me` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates transactions + expenses + users | Calls `ingest_document`. |
| `get_current_user()` | GET | `/v2/users/me` | |
| `list_users(*, cursor, limit, status)` | GET | `/v2/users` | Cursor pagination. |
| `get_user(user_id)` | GET | `/v2/users/{id}` | |
| `list_cards(*, cursor, limit, user_id)` | GET | `/v2/cards` | |
| `get_card(card_id)` | GET | `/v2/cards/{id}` | |
| `list_transactions(*, cursor, limit, posted_at_start)` | GET | `/v2/transactions/card/primary` | Card transactions. |
| `get_transaction(transaction_id)` | GET | `/v2/transactions/card/primary/{id}` | |
| `list_expenses(*, cursor, limit, expense_type, status)` | GET | `/v1/expenses/card` | |
| `get_expense(expense_id)` | GET | `/v1/expenses/card/{id}` | |
| `list_departments(*, cursor, limit)` | GET | `/v2/departments` | |
| `list_locations(*, cursor, limit)` | GET | `/v2/locations` | |
| `list_vendors(*, cursor, limit)` | GET | `/v1/vendors` | |
| `list_receipts(*, cursor, limit)` | GET | `/v1/expenses/card/receipt_match` | |
| `list_budgets(*, cursor, limit)` | GET | `/v2/budgets` | |
| `list_spend_limits(*, cursor, limit)` | GET | `/v2/spend_limits` | |

Wire convention: Brex uses **snake_case** in JSON (`posted_at_date`, `card_id`, `purchased_at`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Brex meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `BrexBadRequestError` (raise) |
| 401 | Access token invalid / missing | `BrexAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (token lacks scope) | `BrexAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `BrexNotFound` (raise) |
| 409 | Conflict | `BrexConflictError` |
| 429 | Rate limited | `BrexRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `BrexServerError` → retry with exponential backoff |

All in `exceptions.py` extending `BrexError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `_BACKOFF_BASE * 2 ** attempt` for 5xx + 429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `access_token` | secret | yes | install_field | Bearer token value |
| `base_url` | text | no | install_field (default `https://platform.brexapis.com`) | Override for sandbox |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["access_token"]
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
| `helpers/normalizer.py` | Maps raw Brex payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Cursor pagination helpers, ISO date parsing. | (stdlib only) |
| `models.py` | Pydantic schemas for Brex resources. | `pydantic` |
| `exceptions.py` | `BrexError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `BrexConnector`. | `connector` |

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
