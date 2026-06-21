# Sage Intacct Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Sage Intacct** is a cloud financial-management platform (AP, AR, GL, projects, HR) whose
public API is a single XML-over-HTTPS endpoint at
`https://api.intacct.com/ia/xml/xmlgw.phtml`. Unlike a REST API, every request is a
full XML envelope that carries **two** credential pairs — a Web-Services *sender*
(partner) and an Intacct *user* — plus a `<companyid>` (Org ID). The response is
also XML, with control/operation/result blocks and per-function `<status>` codes.

This connector — `SageIntacctConnector` (`CONNECTOR_TYPE = "sage_intacct"`,
`AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically
needs from Sage Intacct:

| Surface | Object name | Capability |
|---|---|---|
| Customers | `CUSTOMER` | list / get / create |
| Vendors | `VENDOR` | list / get / create |
| AR Invoices | `ARINVOICE` | list / create |
| AP Bills | `APBILL` | list |
| Journal Entries | `GLBATCH` | list |
| Chart of Accounts | `GLACCOUNT` | list (also used by `health_check`) |
| Employees | `EMPLOYEE` | list |
| Projects | `PROJECT` | list |
| Departments | `DEPARTMENT` | list |
| Locations | `LOCATION` | list |
| Smart Events | n/a | invoke by name |
| Generic | any object | `read_by_query` / `read` / `read_more` |

The connector normalises CUSTOMER / VENDOR / GLACCOUNT records into
`NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces every
user-named operation as a standalone public `async def` (OCP), routes all XML
transport through `client/http_client.py::SageIntacctHTTPClient`, and never
embeds raw HTTP, raw XML, or normalization logic in `connector.py` (SOC).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `pydantic` | `>=2.0` | Schemas for request/response bundles; pre-installed |
| (stdlib) `xml.etree.ElementTree` | — | XML build + parse — no third-party XML lib needed |
| (stdlib) `xml.sax.saxutils.escape` | — | XML-escape user input — no third-party lib needed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

**Deliberate choice:** the gateway returns very predictable XML structures and we
need to *build* envelopes too, so a hand-rolled `helpers/xml_builder.py` is leaner
than pulling in `xmltodict` and dealing with its dict-shape quirks for the
namespaced `<function>` / `<readByQuery>` blocks.

## 3. Auth Flow

Sage Intacct uses **API-key-style multi-credential authentication** — there is
**no OAuth** dance, no token exchange, no refresh.

### Credentials

| Field | Purpose | install_field |
|---|---|---|
| `sender_id` | Web Services partner identity (assigned by Intacct support) | required, string |
| `sender_password` | Web Services partner password | required, secret |
| `user_id` | Intacct user with Web Services privilege | required, string |
| `user_password` | Intacct user password | required, secret |
| `company_id` | Intacct Company / Org ID | required, string |
| `location_id` | Multi-entity location scope | optional, string |
| `entity_id` | Multi-entity entity scope | optional, string |
| `base_url` | Override for sandbox / proxy | optional, string |

### Envelope shape (every request)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<request>
  <control>
    <senderid>{sender_id}</senderid>
    <password>{sender_password}</password>
    <controlid>{uuid}</controlid>
    <uniqueid>false</uniqueid>
    <dtdversion>3.0</dtdversion>
    <includewhitespace>false</includewhitespace>
  </control>
  <operation>
    <authentication>
      <login>
        <userid>{user_id}</userid>
        <companyid>{company_id}</companyid>
        <password>{user_password}</password>
        <locationid>{optional}</locationid>
        <entityid>{optional}</entityid>
      </login>
      <!-- OR, after install() has cached a session_id: -->
      <!-- <sessionid>{session_id}</sessionid> -->
    </authentication>
    <content>
      <function controlid="{uuid}">
        <readByQuery>…</readByQuery>
      </function>
    </content>
  </operation>
</request>
```

### Lifecycle

- `install()` — validates all five required credentials are present, then **calls
  `getAPISession`** against the gateway to mint a `session_id` and stores it via
  `set_metadata("session_id", …)`. The session id is *opportunistic*: when present
  it replaces the `<login>` block; when absent (Redis-less workers, fresh process)
  the connector falls back to full `<login>` credentials. Both shapes work for
  every Intacct function.
- `authorize()` — no-op for api_key; returns a synthetic `TokenInfo` whose
  `access_token` is `f"intacct:{company_id}"` (purely diagnostic).
- `health_check()` — runs a 1-row `readByQuery` on `GLACCOUNT`. On success
  → `HEALTHY + CONNECTED`. XL03* error codes (auth) → `DEGRADED + TOKEN_EXPIRED`.
  Transport / 5xx after retries → `OFFLINE + AUTHENTICATED`.
- `on_token_refresh()` — raises `RefreshError`; Intacct credentials are static.

## 4. Data Model

### 4.1 Customer → NormalizedDocument

| NormalizedDocument | Intacct field | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{CUSTOMERID}"` | tenant-scoped per task brief |
| `source_id` | `CUSTOMERID` | Intacct primary key |
| `title` | `NAME` | |
| `content` | flat `key: value` of all returned fields | full-text indexable |
| `source` | `"sage_intacct.customer"` | |
| `created_at` | `WHENCREATED` (parsed ISO) | |
| `updated_at` | `WHENMODIFIED` (parsed ISO) | |
| `metadata` | `{object: "CUSTOMER", status, **row}` | |

### 4.2 Vendor → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{VENDORID}"` |
| `title` | `NAME` |
| `source` | `"sage_intacct.vendor"` |
| `metadata` | `{object: "VENDOR", status, **row}` |

### 4.3 GL Account → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{ACCOUNTNO}"` |
| `title` | `TITLE` |
| `source` | `"sage_intacct.glaccount"` |
| `metadata` | `{object: "GLACCOUNT", accounttype, **row}` |

## 5. Key API Endpoints & Methods

Every method below exists as a standalone public `async def` in `connector.py`
(OCP). The XML body for each is built by a dedicated helper in
`helpers/xml_builder.py`; transport is owned by
`client/http_client.py::SageIntacctHTTPClient`.

| Method | Function block | Notes |
|---|---|---|
| `install()` | `getAPISession` (best-effort) | Validates config; caches `session_id`. |
| `authorize(auth_code, state)` | (none) | No-op for api_key. |
| `health_check()` | `readByQuery(GLACCOUNT, 1)` | Lightweight credential probe. |
| `sync(since, full, kb_id, webhook_url)` | iterates CUSTOMER + VENDOR + GLACCOUNT | Calls `ingest_document`. |
| `read_by_query(object_name, fields, query, pagesize, returnFormat)` | `readByQuery` | Generic. |
| `read(object_name, keys, fields)` | `read` | Primary-key fetch. |
| `read_more(result_id)` | `readMore` | Page through a prior `readByQuery`. |
| `list_customers(query, pagesize)` | `readByQuery(CUSTOMER)` | |
| `get_customer(customer_id)` | `read(CUSTOMER, [customer_id])` | |
| `create_customer(customer_id, name, status, contact_info)` | `create_customer` | |
| `update_customer(customer_id, fields)` | `update_customer` | Partial patch. |
| `list_vendors(query, pagesize)` | `readByQuery(VENDOR)` | |
| `get_vendor(vendor_id)` | `read(VENDOR, [vendor_id])` | |
| `create_vendor(vendor_id, name, status, contact_info)` | `create_vendor` | |
| `list_invoices(query, pagesize)` | `readByQuery(ARINVOICE)` | |
| `create_invoice(customer_id, invoice_no, invoice_date, due_date, line_items)` | `create_invoice` | At least one line item required. |
| `list_bills(query, pagesize)` | `readByQuery(APBILL)` | |
| `list_journal_entries(query, pagesize)` | `readByQuery(GLBATCH)` | |
| `list_gl_accounts(query, pagesize)` | `readByQuery(GLACCOUNT)` | |
| `list_chart_of_accounts(query, pagesize)` | `readByQuery(GLACCOUNT)` | Alias of `list_gl_accounts`. |
| `list_projects(query, pagesize)` | `readByQuery(PROJECT)` | |
| `list_employees(query, pagesize)` | `readByQuery(EMPLOYEE)` | |
| `list_departments(query, pagesize)` | `readByQuery(DEPARTMENT)` | |
| `list_locations(query, pagesize)` | `readByQuery(LOCATION)` | |
| `run_smart_event(event_name, params)` | `run_smart_event` | |

## 6. Error Handling

Intacct returns HTTP 200 even for application-level failures — the failure is
buried in the XML envelope. The HTTP client retries on 429 / 5xx; the parser
translates XML `<status>failure</status>` blocks to typed exceptions:

| Layer | Trigger | Exception → ConnectorStatus |
|---|---|---|
| HTTP | 401 | `SageIntacctAuthError` → `OFFLINE + TOKEN_EXPIRED` |
| HTTP | 429 / 5xx (after `_MAX_RETRIES`) | `SageIntacctNetworkError` → `OFFLINE + AUTHENTICATED` (still credentialled, provider is down) |
| HTTP | Other 4xx | `SageIntacctError` |
| XML control | `<status>failure</status>` with `errorno` starting `XL03` | `SageIntacctAuthError` → `DEGRADED + TOKEN_EXPIRED` |
| XML control | Any other `<status>failure</status>` | `SageIntacctValidationError` |
| XML function | per-`<result>` failure with `XL03*` | `SageIntacctAuthError` |
| XML function | per-`<result>` failure (other) | `SageIntacctValidationError` (missing field, bad object, query syntax) |
| Transport | `httpx.TransportError` / `TimeoutException` after retries | `SageIntacctNetworkError` |

All exceptions extend `SageIntacctError` (carries `status_code` + `response_body`).

`_STATUS_MAP` on the connector class:

```python
_STATUS_MAP: Dict[int, Any] = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

Retry behaviour (`client/http_client.py`):

| Status | Action |
|---|---|
| 200 | Parse and return |
| 401 | Raise `SageIntacctAuthError` immediately |
| 429 | Exponential backoff (`_BACKOFF_BASE * 2 ** attempt` + jitter, cap `_BACKOFF_MAX`), retry up to `_MAX_RETRIES=3` |
| 5xx | Same backoff, retry up to 3 |
| transport error | Same backoff, retry up to 3 |

## 7. Dependencies

Packages installed by `install_deps` (already pinned in `requirements.txt`):

```
httpx>=0.27.0
structlog>=24.1.0
pytest>=7
pytest-asyncio>=0.23
respx>=0.21
```

Pre-installed in the platform venv (do NOT re-add): `pydantic`, `pytest-mock`.

No `xmltodict` — the platform never needs to round-trip Intacct XML to dicts at
a higher level; the parser at `helpers/xml_builder.py::parse_envelope` produces
the exact narrow dict shape the connector orchestrator consumes.

## 8. Config & Install Fields

Read via `self.config.get(...)` in `connector.py::__init__` — never hardcoded.

| Key | Type | Required | Read in code | Purpose |
|---|---|---|---|---|
| `sender_id` | string | yes | `self.sender_id` | `<senderid>` in `<control>` |
| `sender_password` | secret | yes | `self.sender_password` | `<password>` in `<control>` |
| `user_id` | string | yes | `self.user_id` | `<userid>` in `<login>` |
| `user_password` | secret | yes | `self.user_password` | `<password>` in `<login>` |
| `company_id` | string | yes | `self.company_id` | `<companyid>` in `<login>` |
| `location_id` | string | no | `self.location_id` | `<locationid>` in `<login>` |
| `entity_id` | string | no | `self.entity_id` | `<entityid>` in `<login>` |
| `base_url` | string | no | `self.base_url` (default `https://api.intacct.com/ia/xml/xmlgw.phtml`) | Sandbox / proxy override |
| `rate_limit_per_min` | number | no | `self.rate_limit_per_min` (default 30) | Client-side soft cap |

```python
REQUIRED_CONFIG_KEYS = [
    "sender_id", "sender_password", "user_id", "user_password", "company_id",
]
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Lifecycle (install/authorize/health_check/sync) + 20 public async methods. **No raw HTTP, no XML building, no XML parsing, no normalization.** | `shared.base_connector`, `client.http_client`, `helpers.xml_builder`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx + envelope POST + XML failure classification. Retries 429/5xx with exponential backoff. | `httpx`, `exceptions`, `helpers.xml_builder.parse_envelope` |
| `helpers/xml_builder.py` | XML envelope/function builders + response parser. No HTTP. No business logic. | (stdlib only) |
| `helpers/normalizer.py` | Maps Intacct CUSTOMER / VENDOR / GLACCOUNT rows → `NormalizedDocument`. Tenant-scoped id. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry helper + safe-get for nested dicts. | `exceptions`, `asyncio`, `random` |
| `models.py` | Pydantic-style dataclasses for `IntacctCredentials`, `IntacctFunctionResult`, request bundles. | (stdlib `dataclasses`) |
| `exceptions.py` | `SageIntacctError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `SageIntacctConnector`. | `connector` |

SOC/OCP self-check:

1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. XML build + parse in `helpers/xml_builder.py` ✓
4. Normalization in `helpers/normalizer.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, pagination, normalization) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
