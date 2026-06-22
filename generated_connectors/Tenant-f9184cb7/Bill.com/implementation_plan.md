# Bill.com Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Bill.com** is a SaaS accounts-payable / accounts-receivable platform exposing a REST
API at `https://api.bill.com/api/v2` (legacy v2 — still the most common surface;
newer Connect v3 lives at `https://gateway.prod.bill.com/connect/v3` but the v2
surface covers every operation listed in `plan_steps.json`). This connector —
`BillcomConnector` (`CONNECTOR_TYPE = "billcom"`, `AUTH_TYPE = "api_key"`) — wraps the
operational surfaces a Shielva tenant typically needs from Bill.com:

| Surface | Base path | Capability |
|---|---|---|
| Auth | `/Login.json`, `/Logout.json` | Session exchange (3-piece credential bundle → `sessionId`) |
| Vendors | `/List/Vendor.json`, `/Crud/{Read,Create}/Vendor.json` | List, get, create vendors |
| Customers | `/List/Customer.json`, `/Crud/{Read,Create}/Customer.json` | List, get, create customers |
| Bills (AP) | `/List/Bill.json`, `/Crud/{Read,Create}/Bill.json` | List, get, create bills |
| Invoices (AR) | `/List/Invoice.json`, `/Crud/{Read,Create}/Invoice.json` | List, get, create invoices |
| Payments | `/SendPayment.json`, `/List/SentPay.json`, `/Crud/Read/SentPay.json` | Issue + list + get payments |
| Chart of Accounts | `/List/ChartOfAccount.json` | List ledger accounts |
| Classifications | `/List/ActgClass.json` | List accounting classes |
| Locations | `/List/Location.json` | List entity locations |

The connector normalises vendors + bills + customers + invoices into
`NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone
`async def` methods per user-requested operation (OCP), retries 429/5xx with
exponential backoff (3 attempts), transparently re-logs-in when Bill.com rejects
a stale `sessionId` (BDC_1024 / "Invalid Session."), and never embeds raw HTTP in
`connector.py` (SOC — all HTTP delegated to
`client/http_client.py::BillcomHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

Bill.com has no official Python SDK — `httpx` POST + form-urlencoded is the
canonical wire format.

## 3. Auth Flow — Login + sessionId session model

Bill.com REST API v2 uses **session-based authentication** seeded by a 4-piece
credential bundle (`AUTH_TYPE = "api_key"` at the platform level — the bundle is
treated as a single secret):

### Credentials (install fields)
- `user_name` — Bill.com login email. install_field (type `string`, required).
- `password` — Bill.com login password. install_field (type `secret`, required).
- `org_id` — Bill.com Organization ID, e.g. `00800000000000000`. install_field (type `string`, required).
- `dev_key` — Bill.com Developer Key, issued by Bill.com developer portal. install_field (type `secret`, required).
- `base_url` — Override URL (rare; defaults to `https://api.bill.com/api/v2`). install_field (type `string`, optional).
- `rate_limit_per_min` — Soft client cap (default 60). install_field (type `number`, optional).

### Session lifecycle
| Phase | Behaviour |
|---|---|
| `install()` | Validate `user_name` + `password` + `org_id` + `dev_key`. Run `POST /Login.json` to verify credentials AND obtain the first `sessionId`. Save config + token. |
| `authorize(...)` | No-op — returns the cached `sessionId` as a `TokenInfo`. |
| `health_check()` | Force a fresh `/Login.json` round-trip — minimal probe that confirms the bundle still authenticates. |
| `_ensure_session()` | If no cached `sessionId`, login. Otherwise return the cached value. |
| `_call_with_session(fn)` | Run `fn(session_id)`. On `BillcomSessionExpired`, clear the cache, re-login, retry once. |
| token storage | `sessionId` cached in-process (`self._session_id`) + mirrored as a `TokenInfo` via `set_token` for observability. |

### Wire contract
Every call:

```
POST /<endpoint>.json HTTP/1.1
Content-Type: application/x-www-form-urlencoded

sessionId=<sessionId>&devKey=<devKey>&data=<JSON-encoded-body>
```

`/Login.json` is the only call that does NOT take a `sessionId`; instead it
takes `userName`, `password`, `orgId`, `devKey` directly.

### Envelope
Every response is JSON wrapped in:

```json
{
  "response_status": 0,                  // 0=success, 1=error
  "response_message": "Success",
  "response_data": { ... }               // success payload OR {error_code, error_message}
}
```

`client/http_client.py::_parse_envelope` is the single owner of envelope decoding.
Session-expired error codes (`BDC_1024`, `0001`) and "Invalid Session" message
fragments are surfaced as `BillcomSessionExpired` so the connector layer can
silently re-login.

## 4. Data Model

### 4.1 Vendor → NormalizedDocument

| NormalizedDocument | Bill.com JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{vendor['id']}"` | tenant-scoped |
| `source_id` | `vendor["id"]` | Bill.com ID |
| `title` | `vendor["name"]` | |
| `content` | concat name + email + address | |
| `metadata` | `{email, address1, city, state, zip, country, isActive, kind: "billcom.vendor"}` | |

### 4.2 Bill → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{bill['id']}"` |
| `source_id` | `bill["id"]` |
| `title` | `f"Bill {bill['invoiceNumber']}"` |
| `content` | line-item summary string |
| `metadata` | `{vendorId, invoiceNumber, invoiceDate, dueDate, amount, paymentStatus, kind: "billcom.bill"}` |

### 4.3 Customer → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{customer['id']}"` |
| `source_id` | `customer["id"]` |
| `title` | `customer["name"]` |
| `content` | concat name + email + billing address |
| `metadata` | `{email, billAddress1, kind: "billcom.customer"}` |

### 4.4 Invoice → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{invoice['id']}"` |
| `source_id` | `invoice["id"]` |
| `title` | `f"Invoice {invoice['invoiceNumber']}"` |
| `content` | line-item summary string |
| `metadata` | `{customerId, invoiceNumber, invoiceDate, dueDate, amount, kind: "billcom.invoice"}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST
exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | `/Login.json` | Validate bundle + cache `sessionId`. |
| `authorize(...)` | (lifecycle) | n/a | Surface `sessionId` as `TokenInfo`. |
| `health_check()` | POST | `/Login.json` | Re-run login — proves the bundle still works. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates vendors + bills + customers + invoices | Calls `ingest_document`. |
| `login()` | POST | `/Login.json` | Force fresh login, return new `sessionId`. |
| `logout()` | POST | `/Logout.json` | Invalidate cached `sessionId`. |
| `list_vendors(*, start=0, max=99, filters=None)` | POST | `/List/Vendor.json` | |
| `get_vendor(vendor_id)` | POST | `/Crud/Read/Vendor.json` | |
| `create_vendor(name, email=..., address1=..., city=..., state=..., zip=..., country="US")` | POST | `/Crud/Create/Vendor.json` | |
| `list_bills(*, start=0, max=99, filters=None)` | POST | `/List/Bill.json` | |
| `get_bill(bill_id)` | POST | `/Crud/Read/Bill.json` | |
| `create_bill(vendor_id, invoice_number, invoice_date, due_date, amount, line_items)` | POST | `/Crud/Create/Bill.json` | |
| `pay_bill(bill_id, payment_date)` | POST | `/SendPayment.json` | |
| `list_customers(*, start=0, max=99, filters=None)` | POST | `/List/Customer.json` | |
| `get_customer(customer_id)` | POST | `/Crud/Read/Customer.json` | |
| `create_customer(name, email=..., bill_address1=...)` | POST | `/Crud/Create/Customer.json` | |
| `list_invoices(*, start=0, max=99, filters=None)` | POST | `/List/Invoice.json` | |
| `get_invoice(invoice_id)` | POST | `/Crud/Read/Invoice.json` | |
| `create_invoice(customer_id, invoice_number, invoice_date, due_date, amount, line_items)` | POST | `/Crud/Create/Invoice.json` | |
| `list_payments(*, start=0, max=99, filters=None)` | POST | `/List/SentPay.json` | |
| `get_payment(payment_id)` | POST | `/Crud/Read/SentPay.json` | |
| `list_accounts(*, start=0, max=99)` | POST | `/List/ChartOfAccount.json` | |
| `list_classifications(*, start=0, max=99)` | POST | `/List/ActgClass.json` | |
| `list_locations(*, start=0, max=99)` | POST | `/List/Location.json` | |

Wire convention: Bill.com uses **camelCase** in JSON (`vendorId`, `invoiceNumber`,
`paymentStatus`). The connector boundary accepts/returns these as-is in
`Dict[str, Any]` payloads. Connector-method params are snake_case.

## 6. Error Handling

| HTTP | Bill.com signal | Mapped to |
|---|---|---|
| network error / timeout | transport-level failure | `BillcomNetworkError` → retry up to 3 with exp. backoff |
| 5xx | provider outage | `BillcomNetworkError` → retry up to 3 |
| 429 | rate-limited | `BillcomRateLimitError` → retry with backoff (Bill.com rarely returns this on v2 — it usually surfaces as `response_status=1`) |
| 200 + `response_status=1` + auth error code | `BDC_1011`/`BDC_1018`/`BDC_1019`/`BDC_1020`/`BDC_1021` | `BillcomAuthError` (raise — install / health_check map it) |
| 200 + `response_status=1` + session-expired code | `BDC_1024` / `0001` / "Invalid Session" fragments | `BillcomSessionExpired` (connector silently re-logs-in + retries once) |
| 200 + `response_status=1` + other | unknown failure | `BillcomError` (raise — bubbles to caller) |
| non-JSON body | proxy / outage | `BillcomError` |

All in `exceptions.py` extending `BillcomError`. Retry in
`helpers/utils.py::with_retry` handles transient `BillcomNetworkError` /
`BillcomRateLimitError`. Auth + session errors are NOT retried by `with_retry`
— session errors are handled in `_call_with_session` (silent re-login),
auth errors must reach the operator.

`_STATUS_MAP` on the connector class maps post-classification outcomes back to
the canonical `ConnectorHealth + AuthStatus` pair:

```python
_STATUS_MAP = {
    "auth":    ("OFFLINE",   "INVALID_CREDENTIALS"),
    "session": ("DEGRADED",  "TOKEN_EXPIRED"),
    "network": ("OFFLINE",   "CONNECTED"),
    "rate":    ("DEGRADED",  "CONNECTED"),
}
```

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27,<1.0
structlog>=24.1
```

(pydantic, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `user_name` | text | yes | install_field | `userName` form field on `/Login.json` |
| `password` | secret | yes | install_field | `password` form field on `/Login.json` |
| `org_id` | text | yes | install_field | `orgId` form field on `/Login.json` |
| `dev_key` | secret | yes | install_field | `devKey` form field on every call |
| `base_url` | text | no | install_field (default `https://api.bill.com/api/v2`) | Override for sandbox |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["user_name", "password", "org_id", "dev_key"]
_STATUS_MAP = {
    "auth":    ("OFFLINE",   "INVALID_CREDENTIALS"),
    "session": ("DEGRADED",  "TOKEN_EXPIRED"),
    "network": ("OFFLINE",   "CONNECTED"),
    "rate":    ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no envelope parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx + form-urlencoded encoding + envelope parsing + session-expired detection. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Bill.com payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry, filter normalisation. | (stdlib) + `exceptions` |
| `models.py` | Dataclasses + pydantic schemas for request/response shapes. | `pydantic`, `shared.base_connector` |
| `exceptions.py` | `BillcomError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `BillcomConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP + envelope in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, session re-login, filter normalisation) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
