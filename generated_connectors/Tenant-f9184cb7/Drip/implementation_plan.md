# Drip Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Drip** is an email-marketing + automation platform exposing a v2 REST API
under `https://api.getdrip.com/v2/{account_id}`. This connector —
`DripConnector` (`CONNECTOR_TYPE = "drip"`, `AUTH_TYPE = "api_key"`) — wraps
the operational surfaces a Shielva tenant typically needs from a Drip account:

| Surface | Base path | Capability |
|---|---|---|
| Subscribers | `/subscribers` | List / get / create-or-update / delete subscribers |
| Tags | `/tags` + `/subscribers/{id}/tags/{tag}` | List tags, apply, remove |
| Events | `/events` | Record custom events tied to a subscriber email |
| Orders | `/orders` | List + create orders (Ecommerce automations trigger) |
| Campaigns | `/campaigns` | List + get campaigns, subscribe an email to one |
| Workflows | `/workflows` | List workflows, trigger a workflow for an email |
| Custom Fields | `/custom_field_identifiers` | List custom field keys |
| Broadcasts | `/broadcasts` | List one-off email broadcasts |
| Forms | `/forms` | List email-capture forms |

The connector normalises subscribers + campaigns + orders into
`NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone
`async def` methods per provider-spec operation (OCP), retries 429/5xx with
exponential backoff (3 attempts at the HTTP layer + a caller-side
`with_retry` belt-and-braces), and never embeds raw HTTP in `connector.py`
(SOC — all HTTP delegated to `client/http_client.py::DripHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`,
`respx`.

Connector-specific runtime dependencies declared in `requirements.txt`:

```
httpx>=0.27.0
```

(Listed even though pre-installed in the monorepo venv, so a stand-alone
deploy can resolve it.)

## 3. Auth Flow

Drip uses **HTTP Basic authentication** for server-to-server integrations.

### Credentials
- `api_key` — Drip API key created at **Account → User Settings → API Token**.
  Stored as install_field (type `secret`, required).
- `account_id` — Drip account UUID (the numeric prefix in the dashboard URL,
  e.g. `app.getdrip.com/1234567/…`). install_field (type `string`, required).
- `base_url` — Override of `https://api.getdrip.com/v2` (rarely needed).
- `rate_limit_per_min` — Soft client-side cap (Drip's per-account cap is 3,600/min).

### Header contract
Every request to `https://api.getdrip.com/v2/{account_id}/*`:

```
Authorization: Basic base64({api_key}:)
Accept:        application/json
Content-Type:  application/vnd.api+json
User-Agent:    Shielva-Drip-Connector/1.0
```

### Lifecycle
- `install()` validates `api_key` + `account_id` are non-empty AND round-trips
  `GET /campaigns` so a bad credential fails fast.
- `authorize()` — synthesizes a `TokenInfo(access_token=api_key, type="Basic")`
  to satisfy the BaseConnector contract. No OAuth exchange.
- `health_check()` — `GET /campaigns` lightweight probe.
- `ensure_token()` — N/A (no token lifecycle, no expiry).

## 4. Data Model

### 4.1 Subscriber → NormalizedDocument

| NormalizedDocument | Drip JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{subscriber['id']}"` | tenant-scoped |
| `source_id` | `subscriber["id"]` | Drip subscriber UUID |
| `title` | `first_name + last_name`, fallback `email` | |
| `content` | concat email + first + last + status | |
| `author` | `subscriber["email"]` | |
| `created_at` | `subscriber["created_at"]` | ISO 8601 |
| `metadata` | `{email, status, tags, custom_fields, time_zone, ip_address, kind:"drip.subscriber"}` | |

### 4.2 Campaign → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{campaign['id']}"` |
| `source_id` | `campaign["id"]` |
| `title` | `campaign["name"]` (fallback `Campaign {id}`) |
| `content` | `campaign["subject"]` (fallback `from_name`) |
| `author` | `campaign["from_email"]` |
| `metadata` | `{status, from_name, from_email, subject, kind:"drip.campaign"}` |

### 4.3 Order → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{order['id']}"` |
| `source_id` | `order["id"]` |
| `title` | `f"Order {order['provider_order_id']}"` |
| `content` | `order["financial_state"]` / `order["fulfillment_state"]` |
| `author` | `order["email"]` |
| `metadata` | `{email, provider, amount, currency, financial_state, fulfillment_state, items_count, kind:"drip.order"}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST
exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; round-trip `/campaigns`. |
| `health_check()` | GET | `/campaigns` | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates subscribers + campaigns | Calls `ingest_document`. |
| `list_subscribers(status, page, per_page, subscribed_after, subscribed_before, tags)` | GET | `/subscribers` | Page-based pagination via `meta.total_pages`. |
| `get_subscriber(id_or_email)` | GET | `/subscribers/{id_or_email}` | id OR url-encoded email. |
| `create_or_update_subscriber(email, custom_fields, tags, time_zone, ip_address, first_name, last_name)` | POST | `/subscribers` | Envelope: `{subscribers:[…]}`. |
| `delete_subscriber(id_or_email)` | DELETE | `/subscribers/{id_or_email}` | |
| `list_campaigns(status, page, per_page)` | GET | `/campaigns` | |
| `get_campaign(campaign_id)` | GET | `/campaigns/{id}` | |
| `subscribe_to_campaign(campaign_id, email, double_optin)` | POST | `/campaigns/{id}/subscribers` | Envelope: `{subscribers:[…]}`. |
| `list_workflows(page, per_page)` | GET | `/workflows` | |
| `trigger_workflow(workflow_id, email)` | POST | `/workflows/{id}/subscribers` | Envelope: `{subscribers:[{email}]}`. |
| `record_event(email, action, properties, occurred_at)` | POST | `/events` | Envelope: `{events:[…]}`. |
| `list_orders(page, per_page, occurred_after)` | GET | `/orders` | |
| `create_order(email, provider, provider_order_id, amount, currency, occurred_at, items)` | POST | `/orders` | Envelope: `{orders:[…]}`. |
| `list_tags()` | GET | `/tags` | Returns all account tags. |
| `apply_tag(email, tag)` | POST | `/tags` | Envelope: `{tags:[{email,tag}]}`. |
| `remove_tag(email, tag)` | DELETE | `/subscribers/{email}/tags/{tag}` | URL-encoded email + tag. |
| `list_custom_fields()` | GET | `/custom_field_identifiers` | Account custom field keys. |
| `list_broadcasts(status, page)` | GET | `/broadcasts` | |
| `list_forms()` | GET | `/forms` | |

Wire convention: Drip uses **snake_case** on the wire (`first_name`,
`subscribed_after`, `provider_order_id`). The connector boundary
accepts/returns `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Drip meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `DripBadRequestError` (raise) |
| 401 | API key invalid / missing auth | `DripAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` (install: `INVALID_CREDENTIALS`/`OFFLINE`) |
| 403 | Forbidden (key lacks scope) | `DripAuthError` → `AuthStatus.INVALID_CREDENTIALS` |
| 404 | Not found | `DripNotFoundError` (raise) |
| 409 | Conflict (e.g. duplicate subscription) | `DripConflictError` (raise) |
| 422 | Unprocessable entity | `DripUnprocessableError` (raise) |
| 429 | Rate limited (`Retry-After` header honoured) | `DripRateLimitError` → retried with exponential backoff |
| 5xx | Provider outage | `DripServerError` → retried with exponential backoff |
| transport | timeout / network | `DripNetworkError` → retried |

All in `exceptions.py` extending `DripError`. Retry policy in
`client/http_client.py::_request` uses `_MAX_RETRIES=3` and
`_BACKOFF_BASE=0.5s` (0.5s, 1s, 2s). Caller-side `helpers.utils.with_retry`
re-tries `DripRateLimitError` / `DripServerError` / `DripNetworkError` and
any `DripError` whose `status_code ∈ {429, 500, 502, 503, 504}`.

Back-compat aliases preserved in `exceptions.py`:
```
DripNetworkError = DripServerError   # legacy import name in older callers
DripNotFound     = DripNotFoundError
```

## 7. Dependencies

Connector-specific packages installed in connector's venv (`install_deps`
reads `requirements.txt`):

```
httpx>=0.27.0
```

(`pydantic`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`
are pre-installed in the monorepo venv.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | Drip API key; used as Basic-auth username. Back-compat: connector also accepts `api_token` for previously-installed tenants. |
| `account_id` | string | yes | install_field | Numeric Drip account id (appended to base URL). |
| `base_url` | string | no | install_field (default `https://api.getdrip.com/v2`) | Override (rare). |
| `rate_limit_per_min` | number | no | install_field (default 3600) | Soft client-side cap. |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_key", "account_id"]
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
| `client/http_client.py` | Single owner of `httpx`. Builds headers, retries 429/5xx + transport, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions`, `helpers.utils` |
| `helpers/normalizer.py` | Maps raw Drip payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Basic-auth header builder, subscriber-id encoder, `with_retry`. | `exceptions` (stdlib otherwise) |
| `models.py` | Pydantic schemas for request bodies + response envelopes (reference shapes). | `pydantic` |
| `exceptions.py` | `DripError` hierarchy + back-compat aliases. | (stdlib) |
| `__init__.py` | Re-export `DripConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every spec method is standalone `async def` ✓
7. New ops added without modifying `BaseConnector` ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, encoding) as composable helpers ✓
10. Error mapping in `exceptions.py`; `connector.py` catches custom exceptions only ✓

**Score: 10/10.**
