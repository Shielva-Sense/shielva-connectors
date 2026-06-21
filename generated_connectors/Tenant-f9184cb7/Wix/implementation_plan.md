# Wix Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Wix** is a website-builder + headless CaaS (Commerce-as-a-Service) platform exposing a REST API suite under `https://www.wixapis.com`. This connector — `WixConnector` (`CONNECTOR_TYPE = "wix"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Wix site:

| Surface | Base path | Capability |
|---|---|---|
| Sites | `/sites/v1` | List + read sites under the account |
| Members | `/members/v1` | List + read site members, query by email/status |
| Stores Products | `/stores/v1/products` | List + read catalogue products, inventory |
| Stores Orders | `/stores/v2/orders` | List + read orders, update fulfilment status |
| Bookings | `/bookings/v2` | List bookings, read individual booking |
| Forms | `/forms/v4/submissions` | List form submissions for inbound leads |
| Webhooks | provider-pushed | Verify Wix JWT-signed event callbacks |

The connector normalises members + orders + submissions into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), routes webhook events through `handle_webhook → process_callback (HS256 JWT verify) → _handle_{event}()`.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `PyJWT` | `>=2.8,<3.0` | HS256 JWT verification for Wix webhook signatures |
| `tenacity` | `>=8.2` | Retry decorator for `BandwidthRateLimitError`-style 429 handling |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`.

## 3. Auth Flow

Wix REST API uses **API key authentication** for server-to-server integrations.

### Credentials
- `api_key` — Wix API key created in **Dashboard → Settings → API Keys → Generate API Key**. Stored as install_field (type `secret`, required).
- `account_id` — Wix account UUID (path-segment in some calls). install_field (type `text`, required).
- `site_id` — Specific Wix site UUID to scope operations to. install_field (type `text`, required when calling site-scoped endpoints — Stores, Members, Forms, Bookings).
- `webhook_secret` — Optional HS256 secret for verifying Wix-signed event payloads. install_field (type `secret`, optional).

### Header contract
Every request to `https://www.wixapis.com/*`:

```
Authorization: <api_key>            (no Bearer/Basic prefix)
wix-account-id: <account_id>
wix-site-id:    <site_id>           (omit for account-only endpoints)
Content-Type:   application/json
Accept:         application/json
```

### Lifecycle
- `install()` validates `api_key`, `account_id`, `site_id` are non-empty. Does **not** call the API.
- `authorize()` — NOT implemented (`api_key` flow has no exchange).
- `health_check()` — `GET /sites/v1/sites/{site_id}` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Member → NormalizedDocument

| NormalizedDocument | Wix JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{member['_id']}"` | tenant-scoped |
| `source_id` | `member["_id"]` | Wix member UUID |
| `title` | `member["loginEmail"]` | |
| `content` | concat name + email + phone | |
| `source` | `"wix.members"` | |
| `created_at` | `member["_createdDate"]` | RFC 3339 |
| `updated_at` | `member["_updatedDate"]` | |
| `metadata` | `{status, loginEmail, profile.nickname, contact.phones, ...}` | |

### 4.2 Order → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{order['id']}"` |
| `source_id` | `order["id"]` |
| `title` | `f"Order {order['number']}"` |
| `content` | line-item summary string |
| `source` | `"wix.orders"` |
| `created_at` | `order["createdDate"]` |
| `metadata` | `{status, fulfillmentStatus, paymentStatus, totals, lineItems[].catalogReference, billingInfo.email}` |

### 4.3 Form Submission → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{sub['_id']}"` |
| `source_id` | `sub["_id"]` |
| `title` | `f"Form submission {sub['_id']}"` |
| `content` | `json.dumps(sub["submissions"])` |
| `source` | `"wix.forms"` |
| `created_at` | `sub["_createdDate"]` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/sites/v1/sites/{site_id}` | Lightweight site probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates members + orders + form submissions | Calls `ingest_batch`. |
| `list_sites(page_size=100, cursor=None)` | GET | `/sites/v1/sites?paging.limit={N}&paging.cursor={C}` | Cursor pagination. |
| `get_site(site_id)` | GET | `/sites/v1/sites/{siteId}` | |
| `list_members(*, status=None, limit=100, cursor=None)` | POST | `/members/v1/members/query` | Query API with `paging.limit/cursor`, filter `{status: {$eq: ACTIVE}}`. |
| `get_member(member_id)` | GET | `/members/v1/members/{memberId}` | |
| `create_member(payload)` | POST | `/members/v1/members` | Body: `{member: {...}}`. |
| `list_products(*, limit=100, cursor=None)` | POST | `/stores/v1/products/query` | Cursor-paginated query. |
| `get_product(product_id)` | GET | `/stores/v1/products/{productId}` | |
| `list_orders(*, status=None, limit=100, cursor=None)` | POST | `/stores/v2/orders/search` | Cursor pagination. |
| `get_order(order_id)` | GET | `/stores/v2/orders/{orderId}` | |
| `update_order_status(order_id, fulfillment_status)` | POST | `/stores/v2/orders/{orderId}/update-fulfillment-status` | Body: `{newFulfillmentStatus: ...}`. |
| `list_bookings(*, limit=100, cursor=None)` | POST | `/bookings/v2/bookings/query` | |
| `get_booking(booking_id)` | GET | `/bookings/v2/bookings/{bookingId}` | |
| `list_form_submissions(form_id, *, limit=100, cursor=None)` | POST | `/forms/v4/submissions/query` | Body: `{filter: {formId: {$eq: formId}}, paging: {...}}`. |
| `handle_webhook(payload, headers)` | (lifecycle) | route by `eventType` | Calls `process_callback` first. |
| `process_callback(payload, headers)` | (lifecycle) | HS256 JWT verify | Read `webhook_secret`, decode `payload["data"]` JWT. |
| `handle_event(event)` | (lifecycle) | idempotency-keyed ack | |
| `batch_processor(items)` | (lifecycle) | per-item event processing | |

Wire convention: Wix uses **camelCase** in JSON (`loginEmail`, `siteId`, `fulfillmentStatus`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Wix meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `WixBadRequestError` (raise) |
| 401 | API key invalid / missing header | `WixAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (key lacks permissions) | `WixAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `WixNotFoundError` (raise) |
| 409 | Conflict (e.g. duplicate member email) | `WixConflictError` |
| 428 | Precondition required (revision mismatch) | `WixPreconditionError` |
| 429 | Rate limited (Wix returns no `Retry-After` — default 5s) | `WixRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `WixServerError` → retry with exponential backoff |

All in `exceptions.py` extending `WixError`. Retry in `client/http_client.py::request` honours `max_retries=3`, exponential backoff `min(2 ** attempt, 8)` for 5xx, fixed 5s for 429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
PyJWT>=2.8
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `Authorization` header value |
| `account_id` | text | yes | install_field | `wix-account-id` header |
| `site_id` | text | yes | install_field | `wix-site-id` header; can be empty for account-only ops |
| `webhook_secret` | secret | no | install_field | HS256 secret for `process_callback` JWT verification |
| `timeout_s` | number | no | install_field (default 60) | Per-request httpx timeout |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_key", "account_id", "site_id"]
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
| `helpers/normalizer.py` | Maps raw Wix payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Cursor pagination helpers, ISO date parsing. | (stdlib only) |
| `models.py` | Pydantic schemas with camelCase aliases for request bodies. | `pydantic` |
| `exceptions.py` | `WixError` hierarchy. | (stdlib) |
| `config.py` | pydantic-settings BaseSettings for non-secret runtime knobs only. | `pydantic_settings` |
| `__init__.py` | Re-export `WixConnector`. | `connector` |

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
