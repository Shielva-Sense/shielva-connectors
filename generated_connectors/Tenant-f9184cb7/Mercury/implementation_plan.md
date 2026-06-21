# Mercury Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Mercury** is a US business banking platform exposing a REST API under `https://api.mercury.com/api/v1`. This connector — `MercuryConnector` (`CONNECTOR_TYPE = "mercury"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Mercury organization:

| Surface | Base path | Capability |
|---|---|---|
| Accounts | `/accounts` + `/account/{id}` | List + read accounts under the organization |
| Transactions | `/account/{id}/transactions` | Paginated, date/status/search-filterable ledger |
| Recipients | `/recipients` + `/recipient/{id}` | List + read + create payment recipients |
| Treasury Statements | `/account/{id}/statements` | Monthly statement records for a date range |
| Money Movement | `POST /account/{id}/transactions` | ACH / wire / check sends (Idempotency-Key mandatory) |

The connector normalises accounts + transactions + recipients + statements into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces every method listed in `plan_steps.json::write_connector.config.methods` as a standalone public `async def`, retries 429/5xx with exponential backoff + jitter, and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::MercuryHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

Mercury has **no Python SDK** — direct REST over httpx is the canonical path.

## 3. Auth Flow

Mercury REST API uses **static API token authentication** (server-to-server). Tokens are minted in **Mercury Dashboard → Settings → API Tokens** and may be scoped read-only or read-write.

### Credentials
- `api_token` — Mercury secret token. Stored as install_field (type `secret`, required).
- `default_account_id` — Optional account UUID used by `sync()` and money-movement helpers when no `account_id` is provided per call. install_field (type `text`, optional).
- `base_url` — Optional API host override (default `https://api.mercury.com/api/v1`). install_field (type `text`, optional).
- `rate_limit_per_min` — Soft client-side throttle (default 60). install_field (type `number`, optional).

### Header contract
Every request to `https://api.mercury.com/api/v1/*`:

```
Authorization: Bearer <api_token>
Accept:        application/json
Content-Type:  application/json
Idempotency-Key: <uuid>          (only on money-movement POSTs)
```

### Lifecycle
- `install()` validates `api_token` is non-empty, persists config, then probes `GET /accounts` so a bad token fails install loud (returns `AuthStatus.TOKEN_EXPIRED` on 401).
- `authorize()` — NOT a real exchange (no OAuth). Returns a `TokenInfo` wrapping the static token for ABI compatibility.
- `health_check()` — `GET /accounts` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Account → NormalizedDocument

| NormalizedDocument | Mercury JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{account['id']}"` | tenant-scoped |
| `source_id` | `account["id"]` | Mercury account UUID |
| `title` | `account["name"]` (fallback `nickname`) | |
| `content` | concat `name`, `kind`, balances | |
| `source` | `"mercury.account"` | |
| `created_at` | now() (Mercury accounts have no createdDate field) | |
| `metadata` | `{kind, status, type, availableBalance, currentBalance, routingNumber}` | |

### 4.2 Transaction → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{txn['id']}"` |
| `source_id` | `txn["id"]` |
| `title` | `txn["counterpartyName"]` or `txn["note"]` or `f"Mercury txn {id}"` |
| `content` | `f"{kind} {amount} → {counterparty}"` |
| `source` | `"mercury.transaction"` |
| `created_at` | `txn["createdAt"]` |
| `updated_at` | `txn["postedAt"]` (when present) |
| `metadata` | `{accountId, amount, status, kind, counterpartyName, counterpartyId, note, externalMemo}` |

### 4.3 Recipient → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{rec['id']}"` |
| `source_id` | `rec["id"]` |
| `title` | `rec["name"]` |
| `content` | concat name + emails + payment methods |
| `source` | `"mercury.recipient"` |
| `metadata` | `{status, defaultPaymentMethod, emails, paymentMethods}` |

### 4.4 Statement → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{accountId}_{startDate}_{endDate}"` |
| `source_id` | derived `f"{accountId}-{startDate}-{endDate}"` |
| `title` | `f"Statement {startDate} → {endDate}"` |
| `content` | `json.dumps(items)` |
| `source` | `"mercury.statement"` |

## 5. Key API Endpoints & Methods

Every method MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate `api_token`; init HTTP client. |
| `health_check()` | GET | `/accounts` | Probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates accounts + transactions | Calls `ingest_document`. |
| `list_accounts()` | GET | `/accounts` | |
| `get_account(account_id)` | GET | `/account/{accountId}` | |
| `list_account_transactions(account_id, *, limit=50, offset=0, status, start, end, order, search)` | GET | `/account/{accountId}/transactions` | Mercury's docs name this surface "list transactions". |
| `get_transaction(account_id, transaction_id)` | GET | `/account/{accountId}/transaction/{transactionId}` | |
| `list_recipients()` | GET | `/recipients` | |
| `get_recipient(recipient_id)` | GET | `/recipient/{recipientId}` | |
| `create_recipient(name, emails=None, default_payment_method=None, payment_methods=None)` | POST | `/recipient` | |
| `send_payment(account_id, recipient_id, amount, payment_method, idempotency_key, note=None, external_memo=None)` | POST | `/account/{accountId}/transactions` | Idempotency-Key header mandatory. |
| `list_statements(account_id, start, end)` | GET | `/account/{accountId}/statements?start=&end=` | Date strings are ISO 8601 `YYYY-MM-DD`. |

Wire convention: Mercury uses **camelCase** in JSON (`availableBalance`, `counterpartyName`, `routingNumber`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Mercury meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `MercuryBadRequestError` (raise) |
| 401 | Token missing / invalid | `MercuryAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Token lacks scope (e.g. read-only token attempting POST) | `MercuryAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `MercuryNotFoundError` (raise) |
| 409 | Duplicate / state conflict | `MercuryConflictError` |
| 429 | Rate limited | `MercuryRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `MercuryServerError` → retry with exponential backoff |

All in `exceptions.py` extending `MercuryError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `0.5 * 2 ** attempt` with ±25% jitter, cap 10s.

## 7. Dependencies

```
httpx>=0.27.0
```

(structlog, pydantic, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_token` | secret | yes | install_field | Bearer token value |
| `default_account_id` | text | no | install_field | Default account for `sync()` and money-movement defaults |
| `base_url` | text | no | install_field | Defaults to `https://api.mercury.com/api/v1` |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_token"]
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
| `helpers/normalizer.py` | Maps raw Mercury payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Idempotency-key generator, ISO date parsing, retry wrapper. | (stdlib only) |
| `models.py` | Pydantic schemas with camelCase aliases for request bodies. | `pydantic` |
| `exceptions.py` | `MercuryError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `MercuryConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, idempotency-key) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
