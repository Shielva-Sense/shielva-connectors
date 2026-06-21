# Odoo Connector — Implementation Plan

## 1. Goal

Build a Shielva connector for **Odoo** (open-source ERP + CRM) that talks to a
tenant-specific Odoo instance over JSON-RPC and surfaces the most-used ORM
models (`res.partner`, `crm.lead`, `sale.order`, `account.move`,
`product.template`, `project.task`, `hr.employee`, `stock.picking`) plus a
generic escape hatch.

Same shape as the Wix/Bandwidth connectors:
- `OdooConnector(BaseConnector)` with `CONNECTOR_TYPE="odoo"`, `AUTH_TYPE="api_key"`.
- Public `REQUIRED_CONFIG_KEYS = ["base_url", "db", "username", "api_key"]`.
- Class-level `_STATUS_MAP` for OCP HTTP→(Health, AuthStatus) classification.
- `install()` returns canonical `ConnectorStatus` (no local InstallResult).
- `sync()` returns canonical `SyncResult`; documents flow through
  `NormalizedDocument` with id `f"{tenant_id}_{source_id}"`.
- structlog mandatory; `from shared.base_connector import (...)` — no fallback.
- All HTTP lives in `client/http_client.py::OdooHTTPClient`; `connector.py` is
  orchestration only.

## 2. Auth model

Odoo supports two transports:

1. **JSON-RPC** at `POST {base_url}/jsonrpc` — we use this. Single endpoint, JSON
   bodies, errors carried *inside a 200 OK body* under the `error` key.
2. XML-RPC at `/xmlrpc/2/common` + `/xmlrpc/2/object` — same model semantics.
   Not used here.

JSON-RPC envelope:

```json
{
  "jsonrpc": "2.0",
  "method": "call",
  "params": {
    "service": "object" | "common" | "db",
    "method":  "execute_kw" | "authenticate" | "version",
    "args":    [db, uid, password_or_key, model, method, args, kwargs]
  },
  "id": <opaque>
}
```

### Authentication flow

1. `common.authenticate(db, login, key, {})` → returns integer `uid` on success
   or `false` on bad credentials.
2. Every subsequent call: `object.execute_kw(db, uid, key, model, method, args,
   kwargs)`.

Odoo 14+ supports per-user API keys generated at
*Preferences → Account Security → API Keys*. The connector uses the API key in
place of the user password. This is mandatory when 2FA is enabled — Odoo blocks
JSON-RPC logins that present a password.

The HTTP client caches the `uid` after the first successful `authenticate()` so
subsequent `execute_kw` calls do not re-authenticate on every request.
`AuthError` during `execute_kw` clears the cache so the next call retries.

## 3. Required config

| Key | Type | Required | Purpose |
|---|---|---|---|
| `base_url` | string | yes | Tenant Odoo URL, e.g. `https://mycompany.odoo.com` |
| `db` | string | yes | Odoo database name (from the login page) |
| `username` | string | yes | Odoo login, typically an email |
| `api_key` | secret | yes | Per-user API key from Odoo Preferences |
| `rate_limit_per_min` | number | no | Soft client cap (default 60) |

## 4. Surfaces

Mirrors the eight most-used Odoo ORM models:

| Surface | Model | Methods |
|---|---|---|
| Partners (contacts) | `res.partner` | `list_partners`, `get_partner`, `create_partner`, `update_partner` |
| CRM | `crm.lead` | `list_leads`, `create_lead` |
| Sales | `sale.order` | `list_sale_orders` |
| Invoices | `account.move` (move_type=out_invoice) | `list_invoices` |
| Products | `product.template` | `list_products` |
| Projects | `project.task` | `list_tasks` |
| HR | `hr.employee` | `list_employees` |
| Inventory | `stock.picking` | `list_pickings` |
| Generic | any | `execute_method(model, method, args, kwargs)` |

Each method delegates to `OdooHTTPClient.execute_kw` and wraps it in
`with_retry`. There are no per-model HTTP routes — every operation funnels
through one endpoint and one client method.

## 5. Error handling

Exception hierarchy in `exceptions.py`:

```
OdooError                       # base; carries status_code + response_body
├── OdooAuthError               # 401 / 403 / uid=False / AccessDenied / Session Expired
├── OdooAccessError             # ir.model.access denial (AccessError)
├── OdooNotFoundError           # missing record / 404-style
├── OdooBadRequestError         # 400 / ValidationError
├── OdooRateLimitError          # 429 (Odoo doesn't usually rate-limit but cloud HA might)
├── OdooNetworkError            # transport-level (5xx / timeout / dns)
└── OdooServerError             # alias for 5xx classification
```

`OdooHTTPClient` decodes errors carried inside 200 bodies via `_classify_error`:
- `data.name` contains `AccessDenied` or outer `message` contains
  `Session Expired` → `OdooAuthError`.
- `data.name` contains `AccessError` → `OdooAccessError`.
- `data.name` contains `ValidationError` / `UserError` → `OdooBadRequestError`.
- Everything else → `OdooError`.

`_STATUS_MAP` on the connector translates raw HTTP status to gateway-visible
`(ConnectorHealth, AuthStatus)` for `health_check`:

```python
{
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 6. Retry

`OdooHTTPClient._post` retries on 503 + transport errors with exponential
backoff (base 0.5s, factor 2, max 30s, honors `Retry-After`).

`helpers/utils.py::with_retry` wraps connector orchestration calls for a second
layer of resilience against JSON decode flakiness on intermittent proxies.

## 7. Sync

`sync()` pages through `res.partner` records via repeated `search_read` with
`limit`/`offset`, normalizes each via `helpers/normalizer.py::normalize_partner`
into a `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`) and calls
`self.ingest_document(doc, kb_id=...)`. Returns canonical `SyncResult`.

## 8. Tests

`tests/test_connector.py` uses respx to install a single route on
`{base_url}/jsonrpc` and dispatches based on
`(params.service, params.method, args[3]=model, args[4]=model_method)`.

At minimum:
- install — happy path, bad credentials (uid=False), missing config
- health_check — happy + invalid creds
- list_partners — domain + fields propagation
- create_partner / update_partner
- list_leads / list_invoices (move_type filter)
- error-inside-200 → typed exception
- 503 retry
- uid cache invalidation on AuthError
- class attributes (CONNECTOR_TYPE / AUTH_TYPE / REQUIRED_CONFIG_KEYS)
- multi-tenant isolation

`tests/conftest.py` adds the connector root + monorepo `core` to `sys.path`,
mocks `set_token` / `clear_token` / `save_config` / `ingest_document` /
`get_metadata` / `set_metadata` to prevent base-class Redis/DB side effects,
and mocks `connector.logger` (autouse).

## 9. Folder layout

```
odoo_connector/
├── __init__.py                  # exposes OdooConnector
├── connector.py                 # orchestration only
├── exceptions.py
├── models.py                    # pydantic envelope models
├── requirements.txt
├── client/
│   ├── __init__.py
│   └── http_client.py           # OdooHTTPClient — JSON-RPC, retry, uid cache, error-in-200
├── helpers/
│   ├── __init__.py
│   ├── normalizer.py            # res.partner → NormalizedDocument
│   └── utils.py                 # with_retry
├── metadata/
│   └── connector.json
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   └── test_connector.py
└── .shielva/docs/
    └── connector_docs.json      # 7 doc sections
```
