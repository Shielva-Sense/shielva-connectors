# Lightspeed Retail Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Lightspeed Retail (R-Series POS)** is a cloud point-of-sale platform exposing a REST API under `https://api.lightspeedapp.com/API/V3/Account/{account_id}/`. This connector — `LightspeedConnector` (`CONNECTOR_TYPE = "lightspeed"`, `AUTH_TYPE = "oauth2_code"`) — wraps the operational surfaces a Shielva tenant typically needs from a Lightspeed Retail account:

| Surface | Base path | Capability |
|---|---|---|
| Account | `/Account.json` | Health/identity probe |
| Customers | `/Customer.json` | List, get, create customers (with Contact sub-doc) |
| Items | `/Item.json` | List, get, create catalogue items + Prices envelope |
| Sales | `/Sale.json` | List + read POS sales with date/customer filters |
| Inventory | `/ItemShop.json` | Per-shop stock levels for items |
| Categories | `/Category.json` | Item category tree |
| Vendors | `/Vendor.json` | Supplier registry |
| Employees | `/Employee.json` | Staff registry |
| Shops | `/Shop.json` | Store locations / registers |
| Tax Categories | `/TaxCategory.json` | Tax bracket lookup |
| Discounts | `/Discount.json` | Configured discounts |

The connector normalises items + sales into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), refreshes tokens on 401 (single replay), respects Lightspeed's leaky-bucket header `X-LS-API-Bucket-Level` (proactive backoff when ≥ 90%), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::LightspeedHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

No connector-specific runtime libs beyond the shared baseline — Lightspeed Retail's R-Series API is plain JSON + OAuth2.

## 3. Auth Flow

Lightspeed Retail uses **OAuth 2.0 Authorization Code** flow. Refresh tokens are long-lived (no expiry in practice); access tokens last ~30 min.

### Credentials
- `client_id` — Lightspeed Developer Portal → API → OAuth client ID. install_field (type `string`, required).
- `client_secret` — same portal. install_field (type `secret`, required).
- `account_id` — Lightspeed Retail account ID (numeric). install_field (type `string`, required at runtime for URL composition; stored after install).
- `scopes` — space-separated string. install_field (type `string`, optional, default `"employee:all employee:register"`).
- `auth_url` / `token_url` — override defaults. install_field (type `string`, optional).
- `redirect_uri` — OAuth callback. install_field (type `string`, optional).

### Endpoints
- Authorize: `https://cloud.lightspeedapp.com/oauth/authorize.php`
- Token (exchange + refresh): `https://cloud.lightspeedapp.com/auth/oauth/token`

### Header contract
Every request to `https://api.lightspeedapp.com/API/V3/Account/{account_id}/*`:

```
Authorization: Bearer <access_token>
Accept:        application/json
Content-Type:  application/json    (only on POST/PUT)
```

### Lifecycle
- `install()` validates `client_id`, `client_secret` (and `account_id` for URL composition). Returns `PENDING` until `authorize()`.
- `authorize(auth_code, state)` — POSTs `grant_type=authorization_code` to `/oauth/access_token.php` (alias of `/auth/oauth/token`), stores `TokenInfo`.
- `on_token_refresh()` — POSTs `grant_type=refresh_token`. Long-lived refresh tokens; preserve if response omits a new one.
- `health_check()` — `GET /Account.json` as a lightweight probe; classifies via `_STATUS_MAP`.
- `ensure_token()` — inherited from `BaseConnector`; refreshes when expired.

## 4. Data Model

### 4.1 Item → NormalizedDocument

| NormalizedDocument | Lightspeed JSON | Notes |
|---|---|---|
| `id` | `f"{connector_id}_item_{itemID}"` | tenant-scoped via connector_id |
| `source_id` | `item["itemID"]` | numeric ID as string |
| `title` | `item["description"]` | |
| `content` | `item["description"]` | |
| `source` | `"lightspeed_retail"` | |
| `created_at` | `item["createTime"]` | RFC 3339 |
| `updated_at` | `item["timeStamp"]` | |
| `metadata` | `{item_id, category_id, default_cost, default_price, item_type, custom_sku, manufacturer_sku, tax}` | |

### 4.2 Sale → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{connector_id}_sale_{saleID}"` |
| `source_id` | `sale["saleID"]` |
| `title` | `f"Sale #{saleID} ({completed|open})"` |
| `content` | line summary string |
| `source` | `"lightspeed_retail"` |
| `created_at` | `sale["createTime"]` |
| `metadata` | `{sale_id, customer_id, shop_id, register_id, employee_id, completed, total, discount_percent}` |

### 4.3 Envelope shape

Lightspeed wraps every list response under a top-level resource key (`Item`, `Customer`, `Sale`, …) plus `@attributes` for pagination. `helpers/utils.extract_list()` normalises this to `List[Dict]`.

## 5. Key API Endpoints & Methods

Every method below exists as a standalone public `async def` on `LightspeedConnector`:

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `authorize(auth_code, state)` | POST | `/auth/oauth/token` | Exchange code → TokenInfo. |
| `on_token_refresh()` | POST | `/auth/oauth/token` | grant_type=refresh_token. |
| `health_check()` | GET | `/Account.json` | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates items | Incremental via `timeStamp >,...`. |
| `get_account()` | GET | `/Account.json` | Raw account record. |
| `list_customers(limit, offset, search)` | GET | `/Customer.json` | `lastName` wildcard for search. |
| `get_customer(customer_id)` | GET | `/Customer/{id}.json` | |
| `create_customer(first_name, last_name, email?, phone?, contact?)` | POST | `/Customer.json` | Builds Contact sub-doc. |
| `list_items(limit, offset, search, category_id)` | GET | `/Item.json` | `description` wildcard for search. |
| `get_item(item_id, load_relations?)` | GET | `/Item/{id}.json` | |
| `create_item(description, default_cost, default_price, ...)` | POST | `/Item.json` | Builds Prices envelope. |
| `update_item(item_id, fields)` | PUT | `/Item/{id}.json` | Partial update. |
| `list_sales(limit, offset, completed, start_date, end_date, customer_id)` | GET | `/Sale.json` | `createTime` operators (`>,<,><,`). |
| `get_sale(sale_id, load_relations?)` | GET | `/Sale/{id}.json` | |
| `list_inventory(item_id?, shop_id?)` | GET | `/ItemShop.json` | Per-shop stock. |
| `list_categories(limit, offset)` | GET | `/Category.json` | |
| `list_vendors(limit, offset)` | GET | `/Vendor.json` | |
| `list_employees()` | GET | `/Employee.json` | |
| `list_shops()` | GET | `/Shop.json` | |

## 6. Error Handling

| HTTP | Lightspeed meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `LightspeedBadRequestError` |
| 401 | Token expired / invalid | `LightspeedAuthError` → refresh once, replay; else `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` |
| 403 | Scope insufficient | `LightspeedAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `LightspeedNotFound` |
| 409 | Conflict | `LightspeedConflictError` |
| 429 | Bucket overflow | `LightspeedRateLimitError` → honour `Retry-After`, retry with backoff |
| 5xx | Provider outage | `LightspeedServerError` → exponential backoff, max 4 retries |

All in `exceptions.py` extending `LightspeedError`. Back-compat aliases:
```python
LightspeedNetworkError = LightspeedServerError  # legacy name
```

The HTTP client honours `X-LS-API-Bucket-Level: "current/max"` — at ≥ 90% capacity it sleeps 1s before the next call.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
# All baseline packages (httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx) are pre-installed.
# No connector-specific runtime libs required.
```

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `client_id` | string | yes | install_field | OAuth client ID |
| `client_secret` | secret | yes | install_field | OAuth client secret |
| `account_id` | string | yes (runtime) | install_field | URL segment + persisted on install |
| `scopes` | string | no | install_field (default `"employee:all employee:register"`) | OAuth scope string |
| `auth_url` | string | no | install_field | Override OAuth authorize URL |
| `token_url` | string | no | install_field | Override OAuth token URL |
| `redirect_uri` | string | no | install_field | OAuth callback |
| `rate_limit_per_min` | number | no | install_field (default 50) | Soft cap (informational; the leaky-bucket header is the real enforcer) |

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
| `client/http_client.py` | Single owner of httpx. Bearer auth, refresh-on-401, retry on 429/5xx with `Retry-After`, leaky-bucket throttle. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Lightspeed payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry`, `extract_list`, `parse_lightspeed_datetime`. | (stdlib only) |
| `models.py` | dataclass shims for Item / Customer / Sale + auth/health enum wrappers used in tests. | (stdlib) |
| `exceptions.py` | `LightspeedError` hierarchy. | (stdlib) |
| `__init__.py` | Self-bootstrap sys.path; re-export `LightspeedConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, refresh, bucket-throttle) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
